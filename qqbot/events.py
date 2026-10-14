"""Shared message validation and deduplication for WebSocket and Webhook."""

EVENT_TYPES = {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}


def message_key(event: dict) -> str | None:
    if not isinstance(event, dict):
        raise ValueError("事件不是对象")
    if event.get("op") != 0 or event.get("t") not in EVENT_TYPES:
        return None
    try:
        data = event["d"]
        scene = "c2c" if event["t"] == "C2C_MESSAGE_CREATE" else "group"
        subject = data["author"]["user_openid" if scene == "c2c" else "member_openid"]
        identifiers = [data["id"], subject]
        group = "" if scene == "c2c" else data["group_openid"]
        if scene == "group":
            identifiers.append(group)
        if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in identifiers):
            raise ValueError("事件标识格式错误")
        if not isinstance(data.get("content", ""), str) or not isinstance(data.get("attachments", []), list):
            raise ValueError("事件内容格式错误")
        if not all(isinstance(a, dict) for a in data.get("attachments", [])):
            raise ValueError("附件结构错误")
        return f"{scene}:{group}:{subject}:{data['id']}"
    except (KeyError, TypeError) as exc:
        raise ValueError("事件字段不完整") from exc
