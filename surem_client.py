# -*- coding: utf-8 -*-
"""슈어엠(SureM) API 최소 클라이언트 — 단일 번호 발송 전용"""

import re
import requests

BASE_URL = "https://rest.surem.com"


def clean_phone(phone):
    return re.sub(r"[^0-9]", "", str(phone or ""))


def byte_length(text):
    length = 0
    for ch in text:
        length += 2 if ord(ch) > 127 else 1
    return length


class SuremClient:
    def __init__(self, user_code, secret_key, reg_phone):
        self.user_code = user_code
        self.secret_key = secret_key
        self.reg_phone = clean_phone(reg_phone)
        self._token = None

    def auth(self):
        url = f"{BASE_URL}/api/v1/auth/token"
        res = requests.post(url, json={
            "userCode": self.user_code,
            "secretKey": self.secret_key,
        }, timeout=10)
        data = res.json()
        if data.get("code") == "A0000":
            self._token = data["data"]["accessToken"]
            return True
        raise RuntimeError(f"슈어엠 인증 실패: {data.get('code')} {data.get('message')}")

    def send(self, to, text, subject=""):
        if not self._token:
            self.auth()
        to_clean = clean_phone(to)
        is_lms = byte_length(text) > 90
        path = "/api/v1/send/mms" if is_lms else "/api/v1/send/sms"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        body = {"to": to_clean, "text": text, "reqPhone": self.reg_phone}
        if is_lms and subject:
            body["subject"] = subject
        res = requests.post(BASE_URL + path, headers=headers, json=body, timeout=10)
        data = res.json()
        ok = str(data.get("code", "")) == "A0000"
        return ok, data
