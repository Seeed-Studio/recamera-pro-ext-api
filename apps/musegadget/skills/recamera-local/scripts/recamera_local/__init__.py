"""Stdlib-only Python SDK for a reCamera agent running *on the camera itself*.

Zero configuration on a stock device: the API is probed on
``http://127.0.0.1:80`` then ``https://127.0.0.1:443`` (self-signed), and the
bearer token is read from ``/userdata/config/system/http_key.json``.
Override with ``RECAMERA_HOST`` / ``RECAMERA_PORT`` / ``RECAMERA_PROTOCOL`` /
``RECAMERA_TOKEN``.
"""

from __future__ import annotations

from ._errors import RecameraError
from .acoustic import (
    get_active_acoustic_model,
    list_acoustic_models,
    set_acoustic_model,
)
from .apps import get_app_logs, list_apps, restart_app, start_app, stop_app
from .backup import export_device_config
from .capture import capture_image, get_capture_status, start_capture, stop_capture
from .clock import probe_timezone, sync_system_time
from .detect import (
    clear_detection_events,
    get_detection_events,
    get_detection_model,
    get_detection_models_info,
    get_detection_rules,
    get_detection_schedule,
    set_detection_model,
    set_detection_rules,
    set_detection_schedule,
    wait_event,
)
from .gpio import get_gpio_info, get_gpio_value, list_gpios, set_gpio_value
from .image import get_image_settings, set_image_settings
from .notify import get_notify_config, set_notify_config
from .records import delete_file, list_records, read_file
from .status import (
    get_battery_status,
    get_device_info,
    get_resource_info,
    get_status,
    get_system_time,
    reboot_device,
    set_system_time,
)
from .storage import (
    configure_storage_quota,
    get_storage_status,
    set_storage_slot,
)
from .trigger import (
    activate_http_trigger,
    get_record_config,
    get_record_sources,
    get_record_trigger,
    get_schedule_rule,
    set_record_config,
    set_record_trigger,
    set_schedule_rule,
)
from .video import get_video_encode, set_video_encode

__version__ = "1.0.0"

__all__ = [
    "RecameraError",
    # status / system
    "get_status",
    "get_device_info",
    "get_resource_info",
    "get_system_time",
    "set_system_time",
    "probe_timezone",
    "sync_system_time",
    "get_battery_status",
    "reboot_device",
    "export_device_config",
    # storage
    "get_storage_status",
    "set_storage_slot",
    "configure_storage_quota",
    # capture
    "get_capture_status",
    "start_capture",
    "stop_capture",
    "capture_image",
    # detection
    "get_detection_models_info",
    "get_detection_model",
    "set_detection_model",
    "get_detection_schedule",
    "set_detection_schedule",
    "get_detection_rules",
    "set_detection_rules",
    "get_detection_events",
    "clear_detection_events",
    "wait_event",
    # acoustic (sound-event models)
    "get_active_acoustic_model",
    "list_acoustic_models",
    "set_acoustic_model",
    # record rule system
    "get_record_sources",
    "get_record_config",
    "set_record_config",
    "get_schedule_rule",
    "set_schedule_rule",
    "get_record_trigger",
    "set_record_trigger",
    "activate_http_trigger",
    # records / files
    "list_records",
    "read_file",
    "delete_file",
    # gpio
    "list_gpios",
    "get_gpio_info",
    "set_gpio_value",
    "get_gpio_value",
    # image (ISP)
    "get_image_settings",
    "set_image_settings",
    # app center
    "list_apps",
    "get_app_logs",
    "start_app",
    "stop_app",
    "restart_app",
    # video encode
    "get_video_encode",
    "set_video_encode",
    # result push
    "get_notify_config",
    "set_notify_config",
]
