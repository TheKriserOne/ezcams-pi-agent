"""Decode supported Hailo detection outputs to person boxes.

Faithful port of the proven ``hailo-object-detection-rtsp`` postprocess, minus
the drawing helpers (clips are recorded clean, with no overlay). Supports:

- Hailo NMS-by-class outputs used by the YOLOv11 HEFs.
- Raw YOLO26 class/regression tensors that need Python-side decoding.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Tuple

import numpy as np

PERSON_CLASS_ID = 0  # COCO
_YOLO26_GRID_SIZES = (80, 40, 20)
_YOLO26_STRIDES = {80: 8, 40: 16, 20: 32}

# A person detection is ``(score, [xmin, ymin, xmax, ymax])`` in image pixels.
Detection = Tuple[float, List[int]]


def _denormalize_and_rm_pad(
    box: Iterable[float],
    size: int,
    pad: int,
    img_h: int,
    img_w: int,
) -> List[int]:
    scaled = [int(float(x) * size) for x in box]
    for i in range(4):
        if i % 2 == 0:
            if img_h != size:
                scaled[i] -= pad
        else:
            if img_w != size:
                scaled[i] -= pad
    # [ymin, xmin, ymax, xmax] -> [xmin, ymin, xmax, ymax]
    return [scaled[1], scaled[0], scaled[3], scaled[2]]


def _decode_hailo_nms_detections(
    image_bgr: np.ndarray,
    infer_results,
    score_threshold: float,
    max_boxes: int,
    class_id: int,
) -> List[Detection]:
    img_h, img_w = image_bgr.shape[:2]
    size = max(img_h, img_w)
    pad = int(abs(img_h - img_w) / 2)
    person_dets: List[Detection] = []

    try:
        per_class = infer_results[class_id]
    except Exception:
        return person_dets

    for det in per_class:
        if len(det) < 5:
            continue
        bbox, score = det[:4], float(det[4])
        if score < score_threshold:
            continue
        person_dets.append(
            (score, _denormalize_and_rm_pad(bbox, size, pad, img_h, img_w))
        )

    person_dets.sort(key=lambda x: x[0], reverse=True)
    return person_dets[:max_boxes]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _reverse_letterbox(
    x1: np.ndarray,
    y1: np.ndarray,
    x2: np.ndarray,
    y2: np.ndarray,
    img_h: int,
    img_w: int,
    model_h: int,
    model_w: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    scale = min(model_w / img_w, model_h / img_h)
    scaled_w = max(1, int(img_w * scale))
    scaled_h = max(1, int(img_h * scale))
    x_off = (model_w - scaled_w) // 2
    y_off = (model_h - scaled_h) // 2

    x1 = np.clip((x1 - x_off) / scale, 0, img_w)
    y1 = np.clip((y1 - y_off) / scale, 0, img_h)
    x2 = np.clip((x2 - x_off) / scale, 0, img_w)
    y2 = np.clip((y2 - y_off) / scale, 0, img_h)
    return x1, y1, x2, y2


def _map_yolo26_tensors(infer_results: Dict) -> dict[int, dict[str, np.ndarray]] | None:
    mapped: dict[int, dict[str, np.ndarray]] = {}
    for value in infer_results.values():
        arr = np.asarray(value)
        if arr.ndim == 4 and arr.shape[0] == 1:
            arr = arr[0]
        if arr.ndim != 3 or arr.shape[0] != arr.shape[1]:
            continue
        grid_dim = arr.shape[0]
        if grid_dim not in _YOLO26_GRID_SIZES:
            continue
        channels = arr.shape[2]
        if channels == 80:
            mapped.setdefault(grid_dim, {})["cls"] = arr
        elif channels == 4:
            mapped.setdefault(grid_dim, {})["reg"] = arr

    if all("cls" in mapped.get(g, {}) and "reg" in mapped.get(g, {}) for g in _YOLO26_GRID_SIZES):
        return mapped
    return None


def _decode_yolo26_detections(
    image_bgr: np.ndarray,
    infer_results: Dict,
    score_threshold: float,
    max_boxes: int,
    class_id: int,
    model_w: int,
    model_h: int,
) -> List[Detection]:
    mapped = _map_yolo26_tensors(infer_results)
    if mapped is None:
        return []

    img_h, img_w = image_bgr.shape[:2]
    conf = min(max(score_threshold, 1e-3), 1.0 - 1e-3)
    logit_threshold = -math.log(1.0 / conf - 1.0)
    detections: List[Detection] = []

    for grid_dim in _YOLO26_GRID_SIZES:
        cls_data = mapped[grid_dim]["cls"].reshape(-1, 80)
        reg_data = mapped[grid_dim]["reg"].reshape(-1, 4)

        max_logits = cls_data.max(axis=1)
        class_ids = cls_data.argmax(axis=1)
        mask = (max_logits > logit_threshold) & (class_ids == class_id)
        if not mask.any():
            continue

        indices = np.where(mask)[0]
        scores = _sigmoid(max_logits[indices])
        rows = indices // grid_dim
        cols = indices % grid_dim

        l = reg_data[indices, 0]
        t = reg_data[indices, 1]
        r = reg_data[indices, 2]
        b = reg_data[indices, 3]

        stride = _YOLO26_STRIDES[grid_dim]
        x1 = (cols + 0.5 - l) * stride
        y1 = (rows + 0.5 - t) * stride
        x2 = (cols + 0.5 + r) * stride
        y2 = (rows + 0.5 + b) * stride
        x1, y1, x2, y2 = _reverse_letterbox(
            x1, y1, x2, y2, img_h=img_h, img_w=img_w, model_h=model_h, model_w=model_w
        )

        for i in range(len(indices)):
            detections.append(
                (
                    float(scores[i]),
                    [
                        int(round(float(x1[i]))),
                        int(round(float(y1[i]))),
                        int(round(float(x2[i]))),
                        int(round(float(y2[i]))),
                    ],
                )
            )

    detections.sort(key=lambda x: x[0], reverse=True)
    return detections[:max_boxes]


def extract_person_detections(
    image_bgr: np.ndarray,
    infer_results,
    score_threshold: float = 0.25,
    max_boxes: int = 50,
    class_id: int = PERSON_CLASS_ID,
    model_w: int | None = None,
    model_h: int | None = None,
) -> List[Detection]:
    """Return ``[(score, [xmin, ymin, xmax, ymax])]`` for one COCO class index."""
    if isinstance(infer_results, list) and len(infer_results) == 1:
        infer_results = infer_results[0]

    if isinstance(infer_results, dict):
        if model_w is not None and model_h is not None:
            return _decode_yolo26_detections(
                image_bgr,
                infer_results,
                score_threshold=score_threshold,
                max_boxes=max_boxes,
                class_id=class_id,
                model_w=model_w,
                model_h=model_h,
            )
        return []

    return _decode_hailo_nms_detections(
        image_bgr,
        infer_results,
        score_threshold=score_threshold,
        max_boxes=max_boxes,
        class_id=class_id,
    )
