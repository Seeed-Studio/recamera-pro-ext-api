"""edge-security-kit hub-mode detector pieces, vendored for reCamera Pro.

``letterbox.py`` and ``tracker.py`` are byte-identical copies of
``edge-security-kit/platforms/rknn/esk_rknn/``: the coordinate transform and the
track_id semantics are the two things the MQTT contract pins down hardest, so
the third platform must not re-implement them from the prose.

``zoo_head.py`` keeps the NumPy decode of the ``airockchip/rknn_model_zoo``
YOLOv8 head verbatim and only swaps the runtime wrapper (the reCamera Pro app
gets its ``RKNNLite`` handle from ``kit``, which already loaded the model).

``preview.py`` and ``publisher.py`` are ports: the RK3588 originals depend on
OpenCV, which is not on this Buildroot image. Pillow does the JPEG work here.
"""
