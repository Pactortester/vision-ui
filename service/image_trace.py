import time

import cv2
import os

import numpy
import torch
import clip
import numpy as np
from PIL import Image
from scipy import spatial
from config import CLIP_MODEL_PATH
from service.image_infer import get_ui_infer
from service.image_utils import get_roi_image, img_show, get_image_patches, proposal_fine_tune
from torchvision.transforms import Compose, Resize, CenterCrop, ToTensor, Normalize
import onnxruntime as ort


try:
    from torchvision.transforms import InterpolationMode
    BICUBIC = InterpolationMode.BICUBIC
except ImportError:
    BICUBIC = Image.BICUBIC


def cosine_similar(l1, l2):
    return 1 - spatial.distance.cosine(l1, l2)


def _convert_image_to_rgb(image):
    return image.convert("RGB")


def get_proposals(target_image, source_image_path, provider="ui-infer"):
    """
    选择区域来源，只需提供位置
    """
    # ui-infer，业务应用
    if provider == 'ui-infer':
        image_infer_result = get_ui_infer(source_image_path, 0.2)
    # patches，通用，稀疏元素
    else:
        h, w, _ = target_image.shape
        image_infer_result = get_image_patches(cv2.imread(source_image_path), w, h)
    return image_infer_result


class ImageTrace(object):
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Using {self.device}.")
        self.n_px = 224
        self.template_target_image = np.zeros([100, 100, 3], dtype=np.uint8) + 100
        self.preprocess = self._get_preprocess()
        self.ort_sess = ort.InferenceSession(CLIP_MODEL_PATH)

    def _get_preprocess(self):
        return Compose([
            Resize(self.n_px, interpolation=BICUBIC),
            CenterCrop(self.n_px),
            _convert_image_to_rgb,
            ToTensor(),
            Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)),
        ])

    def search_image(self, target_image_info: dict, source_image_path, top_k, image_alpha, text_alpha):
        top_k = top_k  # 最大匹配数量
        image_alpha = image_alpha  # 图相关系数
        text_alpha = text_alpha  # 文本相关系数
        roi_list = []
        img_text_score = []
        target_image = target_image_info.get('img', self.template_target_image)
        target_image_desc = target_image_info.get('desc', '')
        source_image = cv2.imread(source_image_path)
        proposals = get_proposals(target_image, source_image_path)
        text = clip.tokenize([target_image_desc]).to(self.device)
        # 提取检测目标
        for roi in proposals:
            x1, y1, x2, y2 = list(map(int, roi['elem_det_region']))
            roi = get_roi_image(source_image, [[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
            img_pil = Image.fromarray(cv2.cvtColor(roi, cv2.COLOR_BGR2RGB))
            roi_list.append(self.preprocess(img_pil).to(self.device))
        # 计算图像和文本匹配向量
        img_pil = Image.fromarray(cv2.cvtColor(target_image, cv2.COLOR_BGR2RGB))
        target_image_input = self.preprocess(img_pil).unsqueeze(0).to(self.device).clone().detach()
        source_image_input = torch.tensor(np.stack(roi_list))
        _, logits_per_text,  source_image_features, = self.ort_sess.run(
            ["LOGITS_PER_IMAGE", "LOGITS_PER_TEXT", "onnx::Mul_3493"],
            {"IMAGE": source_image_input.numpy(), "TEXT": text.numpy()}
        )
        probs = torch.from_numpy(logits_per_text).softmax(dim=-1).cpu().numpy()
        if image_alpha != 0:
            target_image_features = self.ort_sess.run(
                ["onnx::Mul_3493"], {"IMAGE": target_image_input.numpy(), "TEXT": text.numpy()}
            )
        else:
            target_image_features = numpy.zeros([len(target_image_input)], dtype=np.uint8)
        # 图像加文本
        for i, source_image_feature in enumerate(source_image_features):
            score = cosine_similar(target_image_features[0], source_image_feature) if image_alpha != 0 else 0
            img_text_score.append(score * image_alpha + probs[0][i] * text_alpha)
        print("Max confidence:", round(np.max(img_text_score) / (image_alpha + text_alpha), 3))
        score_norm = (img_text_score - np.min(img_text_score)) / (np.max(img_text_score) - np.min(img_text_score))
        top_k_ids = np.argsort(score_norm)[-top_k:]
        proposal_fine_tune(score_norm, proposals, 0.8)
        return top_k_ids, score_norm, proposals

    def get_trace_result(self, target_image_info, source_image_path, top_k=3, image_alpha=1.0, text_alpha=0.6):
        top_k_ids, scores, infer_result = self.search_image(target_image_info, source_image_path,
                                                            top_k, image_alpha, text_alpha)
        cls_ids = np.zeros(len(top_k_ids), dtype=int)
        boxes = [infer_result[i]['elem_det_region'] for i in top_k_ids]
        scores = [float(scores[i]) for i in top_k_ids]
        image_show = img_show(cv2.imread(source_image_path), boxes, scores, cls_ids, conf=0.5, class_names=['T'])
        return image_show

    def video_target_track(self, video_path, target_image_info, work_path):
        video_cap = cv2.VideoCapture(video_path)
        _, im = video_cap.read()
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        im_save_path = os.path.join(work_path, 'im_temp.png')
        video_out_path = os.path.join(work_path, 'video_out.mp4')
        out = cv2.VideoWriter(video_out_path, fourcc, 20, (im.shape[1], im.shape[0]))
        i = 0
        while 1:
            i = i + 1
            if i % 2 == 0:
                continue
            print(f"video parsing {i}")
            ret, im = video_cap.read()
            if ret:
                cv2.imwrite(im_save_path, im)
                trace_result = self.get_trace_result(target_image_info, im_save_path, top_k=1)
                out.write(trace_result)
            else:
                print("finish.")
                out.release()
                break


def trace_target_video():
    target_image_info = {
        'path': "./capture/local_images/img_play_icon.png",
        'desc': "picture with play button"
    }
    target_image_info['img'] = cv2.imread(target_image_info['path'])
    video_path = "./capture/local_images/video.mp4"
    work_path = './capture/local_images'
    image_trace = ImageTrace()
    image_trace.video_target_track(video_path, target_image_info, work_path)


def search_target_image():
    """
    # robust target image search
    """
    # 图像目标系数
    image_alpha = 0.0
    # 文本描述系数
    text_alpha = 1.0
    # 最大匹配目标数量
    top_k = 1
    # 调试用，构造目标图像
    target_img = np.zeros([100, 100, 3], dtype=np.uint8)+255
    cv2.putText(target_img, 'Q', (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), thickness=3)
    # target_img = cv2.imread("./capture/local_images/mario.png")
    # 目标语言描述
    desc = "smiling mario"
    target_image_info = {'img': target_img, 'desc': desc}
    source_image_path = "./capture/image_2.png"
    trace_result_path = "./capture/local_images/trace_result.png"
    # 查找目标
    image_trace = ImageTrace()
    t1 = time.time()
    image_trace_show = image_trace.get_trace_result(target_image_info, source_image_path, top_k=top_k,
                                                    image_alpha=image_alpha, text_alpha=text_alpha)
    print(f"Infer time:{round(time.time() - t1, 3)} s", )
    cv2.imwrite(trace_result_path, image_trace_show)
    print(f"Result saved {trace_result_path}")


if __name__ == '__main__':
    assert os.path.exists(CLIP_MODEL_PATH)
    search_target_image()
