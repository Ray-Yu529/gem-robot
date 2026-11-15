"""從 secsgem 的 "communication" logger 旁聽收發的訊息。

secsgem 0.3.0 沒有「每則訊息收／發」的公開 callback，但它把每則訊息連同解碼後的內容
以 INFO 等級記到名為 ``communication`` 的 logger。這裡掛一個 logging.Handler 取用，
不碰 secsgem 的任何私有成員。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

COMMUNICATION_LOGGER = "communication"


@dataclass(frozen=True)
class TappedMessage:
    endpoint: str  # 記錄這則訊息的那一端（secsgem settings.name）
    direction: str  # "tx"：該端送出；"rx"：該端收到
    label: str  # 例如 "S2F41"，或 HSMS 控制訊息名稱
    is_data: bool  # True = SECS-II 資料訊息；False = HSMS 控制訊息（select 等）
    system: int  # system bytes，用來配對 primary / reply
    wbit: bool
    sml: str  # 資料訊息的 SML 文字；控制訊息為空字串


class MessageTap(logging.Handler):
    """把 communication logger 的紀錄轉成 ``TappedMessage`` 丟給 callback。"""

    def __init__(self, callback: Callable[[TappedMessage], None], endpoint: str | None = None) -> None:
        super().__init__(level=logging.INFO)
        self._callback = callback
        self._endpoint = endpoint

    def install(self) -> MessageTap:
        logger = logging.getLogger(COMMUNICATION_LOGGER)
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
        logger.addHandler(self)
        return self

    def uninstall(self) -> None:
        logging.getLogger(COMMUNICATION_LOGGER).removeHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            endpoint = getattr(record, "remoteName", "")
            if self._endpoint is not None and endpoint != self._endpoint:
                return
            message, body = record.args  # type: ignore[misc]
            header = message.header
            is_data = header.s_type.value == 0
            self._callback(
                TappedMessage(
                    endpoint=endpoint,
                    direction="tx" if str(record.msg).startswith(">") else "rx",
                    label=f"S{header.stream}F{header.function}" if is_data else str(body).strip(),
                    is_data=is_data,
                    system=header.system,
                    wbit=bool(getattr(header, "require_response", False)),
                    sml=str(body) if is_data else "",
                ),
            )
        except Exception:  # noqa: BLE001 - logging handler 不可讓例外外洩
            self.handleError(record)
