PLATFORMS = ["android", "ios"]

_DEV = {
    "device": {"type": "string", "description": "serial / udid from mobile_devices (optional with one device)"},
    "platform": {"type": "string", "enum": PLATFORMS, "description": "default android"},
}

_POS = {
    "ref": {"type": "string", "description": "element ref from mobile_ui, e.g. n7"},
    "x": {"type": "integer"},
    "y": {"type": "integer"},
}

READ_ONLY_DEVICE_TOOLS = {"mobile_devices", "mobile_ui", "mobile_screenshot", "mobile_logs"}
