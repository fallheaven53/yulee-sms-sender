# -*- coding: utf-8 -*-
"""
율이공방 — 만족도 조사 문자 발송 웹앱
현장 태블릿에서 번호 입력 → 슈어엠 API 직접 호출 → 즉시 발송
"""

import re
import time
from datetime import datetime

import requests
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

IS_ADMIN = st.query_params.get("admin", "") == "true"

st.set_page_config(
    page_title="2026 토요상설공연 만족도 조사",
    page_icon="📱",
    layout="centered",
    initial_sidebar_state="expanded" if IS_ADMIN else "collapsed",
)

if not IS_ADMIN:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] { display: none !important; }
        [data-testid="collapsedControl"] { display: none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
LOG_SHEET_NAME = "SMS_발송기록"
CONF_SHEET_NAME = "SMS_설정"
LOG_COLS = ["일시", "전화번호", "결과"]

SUREM_BASE_URL = "https://rest.surem.com"
MSG_TEMPLATE = "[광주문화재단] 토요상설공연 만족도 조사에 참여해 주세요.\n{link}"


def clean_phone(phone):
    return re.sub(r"[^0-9]", "", str(phone or ""))


def byte_length(text):
    length = 0
    for ch in text:
        length += 2 if ord(ch) > 127 else 1
    return length


# ══════════════════════════════════════════════════════════════
#  슈어엠 API 직접 호출
# ══════════════════════════════════════════════════════════════

def surem_auth():
    user_code = st.secrets.get("surem_user_code", "")
    secret_key = st.secrets.get("surem_secret_key", "")
    if not user_code or not secret_key:
        return None, "슈어엠 인증정보 미설정"
    url = f"{SUREM_BASE_URL}/api/v1/auth/token"
    body = {"userCode": user_code, "secretKey": secret_key}
    try:
        res = requests.post(url, json=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        }, timeout=10)
    except Exception as e:
        return None, f"네트워크 오류: {e}"
    try:
        data = res.json()
    except Exception:
        return None, f"HTTP {res.status_code} 응답 파싱 실패: {(res.text or '')[:100]}"
    if data.get("code") == "A0000":
        return data["data"]["accessToken"], None
    return None, f"인증 실패: {data.get('code')} {data.get('message', '')}"


def surem_send(token, phone, text, subject=""):
    is_lms = byte_length(text) > 90
    path = "/api/v1/send/mms" if is_lms else "/api/v1/send/sms"
    reg_phone = clean_phone(st.secrets.get("surem_reg_phone", ""))
    body = {"to": clean_phone(phone), "text": text, "reqPhone": reg_phone}
    if is_lms and subject:
        body["subject"] = subject
    try:
        res = requests.post(
            SUREM_BASE_URL + path,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=10,
        )
        data = res.json()
    except Exception as e:
        return False, f"발송 요청 실패: {e}"
    ok = str(data.get("code", "")) == "A0000"
    if ok:
        return True, "성공"
    return False, f"{data.get('code', '')} {data.get('message', '')}"


def send_sms(phone, link):
    token, err = surem_auth()
    if err:
        return False, err
    text = MSG_TEMPLATE.format(link=link)
    ok, result = surem_send(token, phone, text, subject="토요상설공연 만족도조사")
    if not ok and "A1006" in result or "A1001" in result:
        token, err = surem_auth()
        if err:
            return False, err
        ok, result = surem_send(token, phone, text, subject="토요상설공연 만족도조사")
    return ok, result


# ══════════════════════════════════════════════════════════════
#  구글 시트 연결 (발송기록 + 설정)
# ══════════════════════════════════════════════════════════════

@st.cache_resource
def get_sheet():
    if "gcp_service_account" not in st.secrets:
        return None
    sheet_id = st.secrets.get("satisfaction_sheet_id") or st.secrets.get("spreadsheet_id")
    if not sheet_id:
        return None
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)


def _ws(sh, title, header):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title, rows=1000, cols=max(5, len(header)))
        ws.update([header], value_input_option="RAW")
        return ws


def get_form_url():
    default = st.secrets.get("naver_form_url", "")
    sh = get_sheet()
    if sh is None:
        return default
    try:
        ws = _ws(sh, CONF_SHEET_NAME, ["키", "값"])
        rows = ws.get_all_values()
        for row in rows[1:]:
            if len(row) >= 2 and row[0] == "naver_form_url" and row[1]:
                return row[1]
    except Exception:
        pass
    return default


def set_form_url(url):
    sh = get_sheet()
    if sh is None:
        return False
    try:
        ws = _ws(sh, CONF_SHEET_NAME, ["키", "값"])
        rows = ws.get_all_values()
        target = None
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0] == "naver_form_url":
                target = i
                break
        if target:
            ws.update(f"B{target}", [[url]], value_input_option="RAW")
        else:
            ws.append_row(["naver_form_url", url], value_input_option="RAW")
        return True
    except Exception as e:
        st.sidebar.error(f"설정 저장 실패: {e}")
        return False


def log_to_sheet(phone, result):
    sh = get_sheet()
    if sh is None:
        return
    try:
        ws = _ws(sh, LOG_SHEET_NAME, LOG_COLS)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([now, phone, result], value_input_option="RAW")
    except Exception:
        pass


def is_duplicate_today(phone):
    sh = get_sheet()
    if sh is None:
        return False
    try:
        ws = _ws(sh, LOG_SHEET_NAME, LOG_COLS)
        rows = ws.get_all_values()
        today = datetime.now().strftime("%Y-%m-%d")
        for row in rows[1:]:
            if (len(row) >= 3
                    and row[0].startswith(today)
                    and clean_phone(row[1]) == clean_phone(phone)
                    and "성공" in row[2]):
                return True
    except Exception:
        pass
    return False


# ══════════════════════════════════════════════════════════════
#  스타일 (태블릿 큰 UI)
# ══════════════════════════════════════════════════════════════

st.markdown("""
<style>
html, body, [class*="css"] { font-size: 22px !important; }
h1 { font-size: 42px !important; text-align: center; padding-top: 10px; }
.subtitle { font-size: 26px !important; text-align: center; color: #CCCCCC; margin-bottom: 30px; }
.notice { font-size: 24px !important; text-align: center; color: #F5C542; margin: 20px 0; }
input[type="tel"], input[type="text"] {
    font-size: 36px !important; text-align: center; height: 72px !important;
    letter-spacing: 3px;
}
div.stButton > button {
    font-size: 40px !important; height: 140px; width: 100%;
    background-color: #F5C542; color: #111; font-weight: 700;
    border-radius: 16px; border: none;
}
div.stButton > button:hover { background-color: #FFD75E; }
.footer { font-size: 18px !important; text-align: center; color: #888; margin-top: 40px; }
.success-box {
    font-size: 32px !important; text-align: center;
    background: #1b5e20; color: #fff; padding: 40px; border-radius: 16px;
    margin: 30px 0;
}
.error-box {
    font-size: 28px !important; text-align: center;
    background: #b71c1c; color: #fff; padding: 30px; border-radius: 16px;
    margin: 30px 0;
}
.warn-box {
    font-size: 28px !important; text-align: center;
    background: #e65100; color: #fff; padding: 30px; border-radius: 16px;
    margin: 30px 0;
}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  사이드바 — 관리자 페이지
# ══════════════════════════════════════════════════════════════

if IS_ADMIN:
    with st.sidebar:
        st.header("🔧 관리자")
        admin_pw = st.secrets.get("admin_password", "")
        pw_in = st.text_input("비밀번호", type="password")
        if admin_pw and pw_in == admin_pw:
            st.success("관리자 인증")
            cur_url = get_form_url()
            new_url = st.text_input("네이버폼 링크", value=cur_url)
            if st.button("링크 저장"):
                if set_form_url(new_url):
                    st.success("저장 완료")
                    st.rerun()

            if st.button("🔄 캐시 초기화"):
                get_sheet.clear()
                st.rerun()

            st.divider()
            st.caption("📋 오늘 발송 현황")
            sh = get_sheet()
            if sh is not None:
                try:
                    ws = _ws(sh, LOG_SHEET_NAME, LOG_COLS)
                    rows = ws.get_all_values()
                    today = datetime.now().strftime("%Y-%m-%d")
                    todays = [r for r in rows[1:] if r and len(r) >= 3 and r[0].startswith(today)]
                    ok_cnt = len([r for r in todays if "성공" in r[2]])
                    fail_cnt = len([r for r in todays if "성공" not in r[2]])
                    m1, m2 = st.columns(2)
                    m1.metric("성공", ok_cnt)
                    m2.metric("실패", fail_cnt)
                    if todays:
                        import pandas as pd
                        df = pd.DataFrame(todays[-10:], columns=LOG_COLS[:len(todays[-1])])
                        st.caption("최근 10건")
                        st.dataframe(df, use_container_width=True, hide_index=True)
                except Exception as e:
                    st.error(f"기록 조회 실패: {e}")
        elif pw_in:
            st.error("비밀번호가 틀렸습니다.")


# ══════════════════════════════════════════════════════════════
#  메인 화면
# ══════════════════════════════════════════════════════════════

st.markdown("<h1>📋 2026 토요상설공연<br>만족도 조사</h1>", unsafe_allow_html=True)
st.markdown(
    "<div class='subtitle'>전화번호를 입력하시면 설문 링크를<br>문자로 보내드립니다</div>",
    unsafe_allow_html=True,
)

if "status" not in st.session_state:
    st.session_state["status"] = None
    st.session_state["status_time"] = 0
    st.session_state["status_msg"] = ""

status = st.session_state["status"]

if status is not None and time.time() - st.session_state["status_time"] >= 3:
    st.session_state["status"] = None
    st.session_state["status_msg"] = ""
    st.rerun()

if status == "success":
    st.markdown(f"<div class='success-box'>✅ {st.session_state['status_msg']}</div>",
                unsafe_allow_html=True)
    time.sleep(1)
    st.rerun()
elif status == "dup":
    st.markdown(f"<div class='warn-box'>⚠ {st.session_state['status_msg']}</div>",
                unsafe_allow_html=True)
    time.sleep(1)
    st.rerun()
elif status == "error":
    st.markdown(f"<div class='error-box'>❌ {st.session_state['status_msg']}</div>",
                unsafe_allow_html=True)
    time.sleep(1)
    st.rerun()
else:
    admin_hidden = '<input type="hidden" name="admin" value="true">' if IS_ADMIN else ""
    st.html(f"""
    <form method="get" action="" autocomplete="off" style="margin-top: 20px;">
        {admin_hidden}
        <input
            type="tel"
            name="phone"
            inputmode="tel"
            pattern="[0-9]{{10,11}}"
            placeholder="01012345678"
            required
            autofocus
            style="
                width: 100%;
                font-size: 36px;
                text-align: center;
                height: 80px;
                letter-spacing: 3px;
                border-radius: 12px;
                border: 2px solid #555;
                background: #1e1e1e;
                color: #fff;
                padding: 0 16px;
                box-sizing: border-box;
                margin-bottom: 16px;
                outline: none;
            "
        />
        <button
            type="submit"
            style="
                width: 100%;
                font-size: 40px;
                height: 140px;
                background-color: #F5C542;
                color: #111;
                font-weight: 700;
                border-radius: 16px;
                border: none;
                cursor: pointer;
            "
        >📨 전송</button>
    </form>
    """)

    submitted_phone = st.query_params.get("phone", "")
    if submitted_phone:
        try:
            del st.query_params["phone"]
        except KeyError:
            pass

        clean = clean_phone(submitted_phone)
        if len(clean) < 10 or not clean.startswith("01"):
            st.session_state["status"] = "error"
            st.session_state["status_msg"] = "올바른 휴대폰 번호를 입력해주세요"
            st.session_state["status_time"] = time.time()
            st.rerun()
        else:
            if is_duplicate_today(clean):
                st.session_state["status"] = "dup"
                st.session_state["status_msg"] = "이미 발송된 번호입니다"
                st.session_state["status_time"] = time.time()
                st.rerun()
            else:
                form_url = get_form_url()
                if not form_url:
                    st.session_state["status"] = "error"
                    st.session_state["status_msg"] = "네이버폼 링크가 설정되지 않았습니다"
                    st.session_state["status_time"] = time.time()
                    st.rerun()
                else:
                    ok, result = send_sms(clean, form_url)
                    log_to_sheet(clean, result)
                    if ok:
                        st.session_state["status"] = "success"
                        st.session_state["status_msg"] = "문자가 발송되었습니다. 감사합니다!"
                    else:
                        st.session_state["status"] = "error"
                        st.session_state["status_msg"] = f"발송 실패: {result}"
                    st.session_state["status_time"] = time.time()
                    st.rerun()

st.markdown(
    "<div class='footer'>입력하신 번호는 만족도 조사 링크 발송에만 사용됩니다.<br>"
    "발송 기록은 해당 공연일 기준으로만 보관됩니다.</div>",
    unsafe_allow_html=True,
)
