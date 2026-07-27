from typing import Annotated, Any

from fastapi import Header, Request


def device_from_authorization(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    return request.app.state.web.authenticate_device(authorization)
