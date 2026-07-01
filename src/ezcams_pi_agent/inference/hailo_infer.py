"""Minimal async HailoRT inference wrapper.

Faithful port of the proven ``hailo-object-detection-rtsp`` wrapper, which is
itself a trimmed port of ``hailo_apps`` ``HailoInfer``. Uses a shared VDevice
group with the round-robin scheduler so ONE loaded model is time-shared across
every camera stream — this is the core multi-camera optimization. The model is
loaded once; every stream submits async jobs against the same configured model.

``hailo_platform`` is only importable on the Pi (it ships as a Debian package
under ``/usr/lib/python3/dist-packages``). Importing this module off-device
will fail at the import below — that is expected; it only runs on the Pi.
"""
from __future__ import annotations

import os
from functools import partial
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from hailo_platform import HEF, FormatType, HailoSchedulingAlgorithm, VDevice
from hailo_platform.pyhailort.pyhailort import FormatOrder

SHARED_VDEVICE_GROUP_ID = "SHARED"


class HailoInfer:
    def __init__(
        self,
        hef_path: str,
        batch_size: int = 1,
        input_type: Optional[str] = None,
        output_type: Optional[str] = None,
        priority: int = 0,
    ) -> None:
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        params.group_id = SHARED_VDEVICE_GROUP_ID
        self.target = VDevice(params)

        hef_path = os.fspath(hef_path)
        self.hef = HEF(hef_path)
        self.infer_model = self.target.create_infer_model(hef_path)
        self.infer_model.set_batch_size(batch_size)

        self._set_input_type(input_type)
        self._set_output_type(output_type)

        self.config_ctx = self.infer_model.configure()
        self.configured_model = self.config_ctx.__enter__()
        self.configured_model.set_scheduler_priority(priority)
        self.last_infer_job: Optional[object] = None

    def _set_input_type(self, input_type: Optional[str]) -> None:
        if input_type is not None:
            self.infer_model.input().set_format_type(getattr(FormatType, input_type.upper()))

    def _set_output_type(self, output_type: Optional[str]) -> None:
        self.nms_postprocess_enabled = False

        if self.infer_model.outputs[0].format.order == FormatOrder.HAILO_NMS_WITH_BYTE_MASK:
            self.nms_postprocess_enabled = True
            self.output_type = self._output_data_type2dict("UINT8")
            return

        if output_type is None and self._looks_like_yolo26_raw_outputs():
            output_type = "FLOAT32"

        self.output_type = self._output_data_type2dict(output_type)
        for name, dtype in self.output_type.items():
            self.infer_model.output(name).set_format_type(getattr(FormatType, dtype.upper()))

    def get_input_shape(self) -> Tuple[int, ...]:
        return self.hef.get_input_vstream_infos()[0].shape

    def run(
        self,
        input_batch: List[np.ndarray],
        inference_callback_fn: Callable,
    ) -> object:
        bindings_list = self._create_bindings(self.configured_model, input_batch)
        self.configured_model.wait_for_async_ready(timeout_ms=10000)
        self.last_infer_job = self.configured_model.run_async(
            bindings_list,
            partial(inference_callback_fn, bindings_list=bindings_list),
        )
        return self.last_infer_job

    def _create_bindings(self, configured_model, input_batch: List[np.ndarray]):
        def _frame_binding(frame: np.ndarray):
            output_buffers = {
                name: np.empty(
                    self.infer_model.output(name).shape,
                    dtype=(getattr(np, self.output_type[name].lower())),
                )
                for name in self.output_type
            }
            binding = configured_model.create_bindings(output_buffers=output_buffers)
            binding.input().set_buffer(np.ascontiguousarray(frame))
            return binding

        return [_frame_binding(frame) for frame in input_batch]

    def _output_data_type2dict(self, data_type: Optional[str]) -> Dict[str, str]:
        valid_types = {"float32", "uint8", "uint16"}
        type_map: Dict[str, str] = {}
        for info in self.hef.get_output_vstream_infos():
            if data_type is None:
                hef_type = str(info.format.type).split(".")[-1]
                type_map[info.name] = hef_type
            else:
                if data_type.lower() not in valid_types:
                    raise ValueError(
                        f"Invalid data_type: {data_type}. Must be one of {valid_types}"
                    )
                type_map[info.name] = data_type.upper()
        return type_map

    def _looks_like_yolo26_raw_outputs(self) -> bool:
        expected = {
            (80, 80, 4),
            (40, 40, 4),
            (20, 20, 4),
            (80, 80, 80),
            (40, 40, 80),
            (20, 20, 80),
        }
        found = {tuple(info.shape) for info in self.hef.get_output_vstream_infos()}
        return found == expected

    def close(self) -> None:
        if self.last_infer_job is not None:
            try:
                self.last_infer_job.wait(10000)
            except Exception:
                pass
        if self.config_ctx:
            self.config_ctx.__exit__(None, None, None)
