"""Multi-camera inference engine: many streams, ONE shared Hailo model.

Pipeline (generalizes the proven single-stream rtsp app to N cameras):

    N x source threads   ── decode + letterbox (throttled to infer_fps) ─┐
                                                                          v
                                              shared input_queue (bounded)
                                                                          v
    1 x infer thread     ── submit async jobs to ONE HailoInfer ── NPU scheduler
                                                                          v
                                              shared output_queue (bounded)
                                                                          v
    1 x output thread    ── decode detections ── per-camera person state

The model is loaded once; every camera submits against the same configured
model, so the Hailo round-robin scheduler time-shares the single NPU. Inference
is throttled per camera (detection does not need full framerate); the buffer
pool keeps the hot path allocation-free.

Phase 1 logs per-camera person state transitions. Recording hooks into the
source loop (every frame) and the output loop (detection events) in a later
phase.
"""
from __future__ import annotations

import collections
import logging
import queue
import signal
import threading
import time
from functools import partial

import cv2
import numpy as np

from ezcams_pi_agent.inference.config import InferenceConfig
from ezcams_pi_agent.inference.frame_source import AgentStreamSource
from ezcams_pi_agent.inference.postprocess import PERSON_CLASS_ID, extract_person_detections
from ezcams_pi_agent.inference.preprocess import LetterboxPreprocessor, PreprocessBufferPool
from ezcams_pi_agent.inference.recorder import ClipRecorder

log = logging.getLogger("ezcams_pi_agent.inference")

MAX_INPUT_QUEUE_SIZE = 8
MAX_OUTPUT_QUEUE_SIZE = 8
MAX_ASYNC_INFER_JOBS = 2


def _infer_callback(
    completion_info,
    bindings_list,
    *,
    cam_key: str,
    original_bgr: np.ndarray,
    input_rgb: np.ndarray,
    output_queue: queue.Queue,
    buffer_pool: PreprocessBufferPool,
) -> None:
    # Always return the input buffer to the pool, success or failure.
    buffer_pool.release(input_rgb)
    if completion_info.exception:
        log.error("camera %s: inference error: %s", cam_key, completion_info.exception)
        return
    bindings = bindings_list[0]
    if len(bindings._output_names) == 1:
        result = bindings.output().get_buffer()
    else:
        result = {
            name: np.expand_dims(bindings.output(name).get_buffer(), axis=0)
            for name in bindings._output_names
        }
    try:
        output_queue.put_nowait((cam_key, original_bgr, result))
    except queue.Full:
        # Output stage is behind; drop this result rather than block the NPU.
        log.debug("camera %s: output queue full, dropping result", cam_key)


def _source_loop(
    cam_key: str,
    source: AgentStreamSource,
    input_queue: queue.Queue,
    model_w: int,
    model_h: int,
    infer_fps: float,
    buffer_pool: PreprocessBufferPool,
    recorder: ClipRecorder | None,
    stop_event: threading.Event,
) -> None:
    preprocessor = LetterboxPreprocessor(model_w, model_h)
    infer_interval = 1.0 / infer_fps if infer_fps > 0 else 0.0
    next_infer_at = 0.0
    for jpeg in source.jpeg_frames():
        if stop_event.is_set():
            break
        if recorder is not None:
            recorder.feed(jpeg)  # every frame -> clip when recording (no decode)
        now = time.monotonic()
        if now < next_infer_at:
            continue  # throttle inference only
        next_infer_at = now + infer_interval

        frame_bgr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame_bgr is None:
            continue
        try:
            input_rgb = buffer_pool.acquire(timeout=0.1)
        except queue.Empty:
            continue  # backpressure: NPU/pipeline busy, drop this frame
        preprocessor.prepare_into(frame_bgr, input_rgb)
        try:
            input_queue.put((cam_key, frame_bgr, input_rgb), timeout=0.25)
        except queue.Full:
            buffer_pool.release(input_rgb)
    log.info("camera %s: source loop stopped", cam_key)


def _infer_loop(
    hailo,
    input_queue: queue.Queue,
    output_queue: queue.Queue,
    buffer_pool: PreprocessBufferPool,
    stop_event: threading.Event,
) -> None:
    pending: collections.deque = collections.deque()
    while True:
        item = input_queue.get()
        if item is None:
            break
        cam_key, original_bgr, input_rgb = item
        if stop_event.is_set():
            buffer_pool.release(input_rgb)
            continue
        cb = partial(
            _infer_callback,
            cam_key=cam_key,
            original_bgr=original_bgr,
            input_rgb=input_rgb,
            output_queue=output_queue,
            buffer_pool=buffer_pool,
        )
        while len(pending) >= MAX_ASYNC_INFER_JOBS:
            pending.popleft().wait(10000)
        pending.append(hailo.run([input_rgb], cb))
    for job in pending:
        try:
            job.wait(10000)
        except Exception:
            pass
    hailo.close()
    output_queue.put(None)


def _output_loop(
    output_queue: queue.Queue,
    cfg: InferenceConfig,
    model_w: int,
    model_h: int,
    recorders: dict[str, ClipRecorder],
    stop_event: threading.Event,
) -> None:
    present: dict[str, bool] = {}
    streak: dict[str, int] = {}
    while True:
        item = output_queue.get()
        if item is None:
            break
        cam_key, original_bgr, result = item
        dets = extract_person_detections(
            original_bgr,
            result,
            score_threshold=cfg.score_threshold,
            max_boxes=cfg.max_boxes,
            class_id=PERSON_CLASS_ID,
            model_w=model_w,
            model_h=model_h,
        )
        is_present = len(dets) > 0

        # Debounce, then arm/extend the clip while a person stays present.
        if is_present:
            streak[cam_key] = streak.get(cam_key, 0) + 1
            if streak[cam_key] >= cfg.trigger_frames:
                rec = recorders.get(cam_key)
                if rec is not None:
                    rec.note_detection()
        else:
            streak[cam_key] = 0

        was_present = present.get(cam_key, False)
        if is_present != was_present:
            present[cam_key] = is_present
            if is_present:
                log.info(
                    "camera %s: PERSON detected (%d, top score %.2f)",
                    cam_key,
                    len(dets),
                    dets[0][0],
                )
            else:
                log.info("camera %s: person gone", cam_key)
        else:
            log.debug("camera %s: %d person(s)", cam_key, len(dets))


def run_engine(cfg: InferenceConfig) -> int:
    # Imported here so the module stays importable off-device (no hailo_platform).
    from ezcams_pi_agent.inference.hailo_infer import HailoInfer

    if not cfg.camera_keys:
        log.error("No cameras to watch. Nothing to do.")
        return 2

    log.info("Loading HEF: %s", cfg.hef_path)
    hailo = HailoInfer(str(cfg.hef_path))
    model_h, model_w, _ = hailo.get_input_shape()
    log.info("Model input: %dx%d | cameras: %s", model_w, model_h, ", ".join(cfg.camera_keys))

    stop_event = threading.Event()

    def _sig_handler(signum, _frame) -> None:
        log.info("Signal %s received; shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    pool_size = MAX_INPUT_QUEUE_SIZE + MAX_ASYNC_INFER_JOBS + len(cfg.camera_keys)
    buffer_pool = PreprocessBufferPool(model_w, model_h, pool_size)
    input_queue: queue.Queue = queue.Queue(MAX_INPUT_QUEUE_SIZE)
    output_queue: queue.Queue = queue.Queue(MAX_OUTPUT_QUEUE_SIZE)

    recorders: dict[str, ClipRecorder] = {
        cam_key: ClipRecorder(
            cam_key,
            cfg.clip_dir,
            min_clip_seconds=cfg.min_clip_seconds,
            grace_seconds=cfg.grace_seconds,
        )
        for cam_key in cfg.camera_keys
    }
    log.info("Clips -> %s", cfg.clip_dir.resolve())

    infer_t = threading.Thread(
        target=_infer_loop,
        args=(hailo, input_queue, output_queue, buffer_pool, stop_event),
        name="infer",
        daemon=True,
    )
    out_t = threading.Thread(
        target=_output_loop,
        args=(output_queue, cfg, model_w, model_h, recorders, stop_event),
        name="output",
        daemon=True,
    )

    source_threads: list[threading.Thread] = []
    for cam_key in cfg.camera_keys:
        source = AgentStreamSource(cfg.agent_url, cam_key, stop_event)
        t = threading.Thread(
            target=_source_loop,
            args=(
                cam_key,
                source,
                input_queue,
                model_w,
                model_h,
                cfg.infer_fps,
                buffer_pool,
                recorders[cam_key],
                stop_event,
            ),
            name=f"source-{cam_key}",
            daemon=True,
        )
        source_threads.append(t)

    infer_t.start()
    out_t.start()
    for t in source_threads:
        t.start()
    log.info("Inference engine running (%d cameras). Ctrl-C to stop.", len(source_threads))

    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        stop_event.set()
        for t in source_threads:
            t.join(timeout=5.0)
        input_queue.put(None)  # end the infer loop
        infer_t.join(timeout=15.0)
        out_t.join(timeout=5.0)
        for rec in recorders.values():
            rec.stop()  # finalize any open clip

    log.info("Stopped cleanly.")
    return 0
