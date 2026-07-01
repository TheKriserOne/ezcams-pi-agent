"""On-device YOLO inference + clip recording for the EZ Cams Pi agent.

Runs as a SEPARATE process from the streaming agent. It consumes the agent's
already-decoded MJPEG frames over loopback, runs a single shared Hailo model
across all cameras (the NPU scheduler time-shares the streams), and records
clips when a person is detected.

The streaming/live-view path in :mod:`ezcams_pi_agent.main` is unchanged: app
users keep seeing raw camera frames with no computer-vision overlay.
"""
