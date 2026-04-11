# -*- coding: utf-8 -*-
"""
율이공방 — 만족도 조사 문자 자동 발송 웹앱
현장 태블릿(10인치)에서 번호 입력 → 즉시 문자 발송
"""

import re
import time
from datetime import datetime

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

from surem_client import SuremClient, clean_phone

st.set_page_config(
    page_title="2026 토요상설공연 만족도 조사",
    page_icon="📱",
    layout="centered",
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
LOG_SHEET_NAME = "SMS_발송기록"
CONF_SHEET_NAME = "SMS_설정"

DEFAULT_MSG_TEMPLATE = (
    "[광주문화재단] 토요상설공연 만족도 조사에 참여해 주세요.\n{link}"
)

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
    """설정 시트에서 네이버폼 링크 조회 (없으면 secrets 기본값)"""
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


def already_sent_today(phone):
    sh = get_sheet()
    if sh is None:
        return False
    try:
        ws = _ws(sh, LOG_SHEET_NAME, ["일시", "전화번호", "결과"])
        rows = ws.get_all_values()
        today = datetime.now().strftime("%Y-%m-%d")
        clean = clean_phone(phone)
        for row in rows[1:]:
            if len(row) >= 3 and row[0].startswith(today) and clean_phone(row[1]) == clean and "성공" in row[2]:
                return True
    except Exception:
        pass
    return False


def log_send(phone, result_text):
    sh = get_sheet()
    if sh is None:
        return
    try:
        ws = _ws(sh, LOG_SHEET_NAME, ["일시", "전화번호", "결과"])
        ws.append_row(
            [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), clean_phone(phone), result_text],
            value_input_option="RAW",
        )
    except Exception as e:
        st.warning(f"발송 기록 저장 실패: {e}")


# ══════════════════════════════════════════════════════════════
#  슈어엠 클라이언트
# ══════════════════════════════════════════════════════════════

@st.cache_resource
def get_surem():
    try:
        return SuremClient(
            user_code=st.secrets["surem_user_code"],
            secret_key=st.secrets["surem_secret_key"],
            reg_phone=st.secrets["surem_reg_phone"],
        )
    except Exception as e:
        st.error(f"슈어엠 설정 오류: {e}")
        return None


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

        if st.button("🔄 발송기록 캐시 초기화"):
            get_sheet.clear()
            st.rerun()

        st.divider()
        st.caption("🔍 슈어엠 연결 진단")
        uc = st.secrets.get("surem_user_code", "")
        sk = st.secrets.get("surem_secret_key", "")
        rp = st.secrets.get("surem_reg_phone", "")
        st.text(f"user_code: {uc[:4]}***{uc[-2:] if len(uc)>6 else ''}")
        st.text(f"secret_key: {'설정됨' if sk else '❌ 비어있음'} (길이 {len(sk)})")
        st.text(f"reg_phone: {rp}")
        if st.button("🧪 슈어엠 인증 테스트"):
            try:
                from surem_client import SuremClient
                c = SuremClient(uc, sk, rp)
                c.auth()
                st.success(f"인증 성공 · 토큰 앞 8자: {c._token[:8]}")
            except Exception as e:
                st.error(str(e))

        st.divider()
        st.caption("오늘 발송 이력")
        sh = get_sheet()
        if sh is not None:
            try:
                ws = _ws(sh, LOG_SHEET_NAME, ["일시", "전화번호", "결과"])
                rows = ws.get_all_values()
                today = datetime.now().strftime("%Y-%m-%d")
                todays = [r for r in rows[1:] if r and r[0].startswith(today)]
                st.metric("오늘 발송 건수", len(todays))
                if todays:
                    import pandas as pd
                    df = pd.DataFrame(todays, columns=["일시", "전화번호", "결과"])
                    st.dataframe(df, use_container_width=True, hide_index=True)
            except Exception:
                pass
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

# 상태 관리
if "status" not in st.session_state:
    st.session_state["status"] = None  # None / "success" / "error" / "dup"
    st.session_state["status_time"] = 0
    st.session_state["status_msg"] = ""

status = st.session_state["status"]

# 자동 리셋 (3초 후)
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
    # 입력 폼
    with st.form("sms_form", clear_on_submit=True):
        phone = st.text_input(
            "전화번호",
            placeholder="01012345678",
            key="phone_input",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("📨 전송")

    if submitted:
        clean = clean_phone(phone)
        if len(clean) < 10 or not clean.startswith("01"):
            st.session_state["status"] = "error"
            st.session_state["status_msg"] = "올바른 휴대폰 번호를 입력해주세요"
            st.session_state["status_time"] = time.time()
            st.rerun()
        elif already_sent_today(clean):
            st.session_state["status"] = "dup"
            st.session_state["status_msg"] = "이미 오늘 발송되었습니다"
            st.session_state["status_time"] = time.time()
            log_send(clean, "중복차단")
            st.rerun()
        else:
            form_url = get_form_url()
            if not form_url:
                st.session_state["status"] = "error"
                st.session_state["status_msg"] = "네이버폼 링크가 설정되지 않았습니다"
                st.session_state["status_time"] = time.time()
                st.rerun()
            else:
                surem = get_surem()
                if surem is None:
                    st.session_state["status"] = "error"
                    st.session_state["status_msg"] = "문자 서비스 연결 실패"
                    st.session_state["status_time"] = time.time()
                    st.rerun()
                else:
                    msg = DEFAULT_MSG_TEMPLATE.format(link=form_url)
                    try:
                        ok, data = surem.send(clean, msg, subject="토요상설공연 만족도조사")
                        if ok:
                            st.session_state["status"] = "success"
                            st.session_state["status_msg"] = "문자가 발송되었습니다. 감사합니다!"
                            log_send(clean, "성공")
                        else:
                            st.session_state["status"] = "error"
                            st.session_state["status_msg"] = f"발송 실패 ({data.get('message','')})"
                            log_send(clean, f"실패({data.get('code','')})")
                    except Exception as e:
                        st.session_state["status"] = "error"
                        st.session_state["status_msg"] = f"발송 중 오류: {e}"
                        log_send(clean, f"오류")
                    st.session_state["status_time"] = time.time()
                    st.rerun()

st.markdown(
    "<div class='footer'>입력하신 번호는 만족도 조사 링크 발송에만 사용됩니다.<br>"
    "발송 기록은 해당 공연일 기준으로만 보관됩니다.</div>",
    unsafe_allow_html=True,
)
