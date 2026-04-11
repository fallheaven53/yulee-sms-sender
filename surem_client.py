# -*- coding: utf-8 -*-
"""슈어엠(SureM) API 최소 클라이언트 — 단일 번호 발송 전용"""

import re
import json
import requests

BASE_URL = "https://rest.surem.com"


def clean_phone(phone):
    return re.sub(r"[^0-9]", "", str(phone or ""))


def byte_length(text):
    length = 0
    for ch in text:
        length += 2 if ord(ch) > 127 else 1
    return length


def _parse(res, stage):
    """응답을 JSON으로 파싱. 실패 시 진단 정보가 담긴 RuntimeError."""
    try:
        return res.json()
    except json.JSONDecodeError:
        body = (res.text or "").strip()[:200]
        raise RuntimeError(
            f"[{stage}] HTTP {res.status_code} · JSON 아님 · 응답: {body or '(빈 응답)'}"
        )


class SuremClient:
    def __init__(self, user_code, secret_key, reg_phone):
        self.user_code = user_code
        self.secret_key = secret_key
        self.reg_phone = clean_phone(reg_phone)
        self._token = None

    def auth(self):
        url = f"{BASE_URL}/api/v1/auth/token"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        body = {"userCode": self.user_code, "secretKey": self.secret_key}
        try:
            res = requests.post(url, headers=headers, json=body, timeout=15)
        except requests.RequestException as e:
            raise RuntimeError(f"[auth] 네트워크 오류: {e}")
        data = _parse(res, "auth")
        if data.get("code") == "A0000":
            self._token = data["data"]["accessToken"]
            return True
        raise RuntimeError(
            f"[auth] 인증 실패 HTTP {res.status_code} · "
            f"code={data.get('code')} · msg={data.get('message')}"
        )

    def send(self, to, text, subject=""):
        if not self._token:
            self.auth()
        to_clean = clean_phone(to)
        is_lms = byte_length(text) > 90
        path = "/api/v1/send/mms" if is_lms else "/api/v1/send/sms"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        body = {"to": to_clean, "text": text, "reqPhone": self.reg_phone}
        if is_lms and subject:
            body["subject"] = subject
        try:
            res = requests.post(BASE_URL + path, headers=headers, json=body, timeout=15)
        except requests.RequestException as e:
            raise RuntimeError(f"[send] 네트워크 오류: {e}")
        data = _parse(res, "send")
        ok = str(data.get("code", "")) == "A0000"
        return ok, data
