from pathlib import Path
from inspect import signature
import sys
import re

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st
from src.config import settings
from src.services.request_service import STATUS_NAMES as REQUEST_STATUSES
from src.ui.api_client import APIError, get_api_client
from src.ui.request_management import render_request_list, render_management, render_history, clear_selection
from src.ui.presentation import EVENT_NAMES, amount, business_text, error_message, evidence_rows, local_time, reason_name, status_name
from src.ui.presentation import MISSING_FIELD_LABELS, decision_deadline, proposal_facts, reason_needs_review

st.set_page_config(page_title="售后工作台", page_icon="●", layout="wide")
st.markdown('''<style>
.stApp { background: #f8f9fa; }
.block-container { max-width: 1180px; padding-top: 2rem; }
[data-testid="stMetric"] { background: white; padding: 14px; border: 1px solid #e2e5e9; border-radius: 8px; }
.stButton > button, .stFormSubmitButton > button { border-radius: 7px; }
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"] { display: none; }
</style>''', unsafe_allow_html=True)
st.title("售后工作台")
api = get_api_client()
try:
    api.get("/ready")
except APIError:
    st.info("服务正在启动或暂时不可用。等待片刻后点击重新连接；不会自动重试提交。")
    if st.button("重新连接"):
        st.rerun()
    st.stop()

for key, value in {"selected_proposal": None, "unknown_execution": [], "new_request": False,
                   "request_page": 0, "ticket_page": 0, "selected_ticket": None}.items():
    st.session_state.setdefault(key, value)


def notify(message, kind="info"):
    st.session_state["notice"] = (kind, message)


def show_error(exc, proposal_id=None):
    if proposal_id and exc.uncertain:
        st.session_state["unknown_execution"] = list(set(st.session_state["unknown_execution"] + [proposal_id]))
    if exc.details.get("proposal_id"):
        st.session_state["selected_proposal"] = exc.details["proposal_id"]
        st.session_state["new_request"] = False
    notify(error_message(exc.code, uncertain=exc.uncertain), "warning" if exc.uncertain else "error")


def verify_execution(proposal_id):
    try:
        value = api.get(f"/proposals/{proposal_id}/execution")
        if value["status"] == "executed":
            if not all(value.get("verification", {}).get(key) is True for key in
                       ("ticket_row_found", "proposal_executed", "success_audit_found")):
                raise APIError("VERIFICATION_MISMATCH", "核验未通过", uncertain=True)
            ticket = api.get("/aftersales-tickets/" + value["ticket"]["id"])
            if any(ticket.get(key) != item for key, item in value["ticket"].items()):
                raise APIError("VERIFICATION_MISMATCH", "核验未通过", uncertain=True)
        elif value["status"] != "not_executed":
            raise APIError("EXECUTION_UNKNOWN", "结果待核验", uncertain=True)
    except APIError as exc:
        raise APIError(exc.code, exc.message, details=exc.details, uncertain=True) from None
    st.session_state["unknown_execution"] = [item for item in st.session_state["unknown_execution"] if item != proposal_id]
    if value['status'] == 'not_executed':
        st.session_state['known_not_executed'] = list(set(st.session_state.get('known_not_executed', []) + [proposal_id]))
    return value


def open_ticket(ticket_id):
    st.session_state["selected_ticket"] = ticket_id
    st.session_state["workspace"] = "售后工单"


def select_proposal(value):
    st.session_state["selected_proposal"] = value["proposal_id"]
    st.session_state["new_request"] = False


def fill_order(order_id, single_order=False):
    question = st.session_state.get("question", "")
    if single_order:
        question = re.sub(r"(?<![A-Za-z0-9])ORD-[A-Z0-9-]+", "", question, flags=re.IGNORECASE).strip()
    existing = set(re.findall(r"(?<![A-Za-z0-9])ORD-[A-Z0-9-]+", question.upper()))
    if existing and existing != {order_id}:
        notify("当前问题已有其他订单号，请先核对并修改，原文未被替换。", "warning")
        return
    if not existing:
        filled = question.rstrip() + ("\n" if question.strip() else "") + "订单号：" + order_id
        if len(filled) > 4000:
            notify("当前问题已达到字数上限，请精简后再选入订单号，原文未被修改。", "warning")
            return
        st.session_state["question"] = filled
    notify("订单号已填入本页，尚未保存。请核对后提交分析。")


def order_lookup(*, expanded=False, single_order=False):
    with st.expander("查找订单", expanded=expanded):
        query = st.text_input("订单号或商品名称", key="order_search", max_chars=200,
                              placeholder="例如：音箱、键盘，或订单号的一部分")
        if st.session_state.get("order_filters") != query:
            st.session_state["order_page"] = 0
            st.session_state["order_filters"] = query
            st.session_state.pop("order_picker", None)
        page = st.session_state.get("order_page", 0)
        try:
            data = api.get("/orders", params={"q": query, "limit": 5, "offset": page * 5})
        except APIError as exc:
            st.info(error_message(exc.code))
            return
        rows = data["items"]
        if not rows:
            st.info("没有找到订单，请尝试商品名称或订单号的一部分，也可以手动填写订单号。")
        else:
            if not expanded or st.checkbox("查看候选订单详情", key="show_order_details"):
                st.table([{"订单号": row["order_id"], "商品": "、".join(row["product_names"]),
                    "订单状态": status_name(row["status"]), "金额": amount(row["total_cents"], row["currency"]),
                    "签收时间": local_time(row["delivered_at"], settings.app_timezone),
                    "退货时限": row["return_window_label"]} for row in rows])
            choices = [None] + [row["order_id"] for row in rows]
            if st.session_state.get("order_picker") not in choices:
                st.session_state.pop("order_picker", None)
            labels = {row["order_id"]: f"{row['order_id']} · {'、'.join(row['product_names'])} · {status_name(row['status'])}"
                      for row in rows}
            selected = st.selectbox("选择订单", choices, format_func=lambda value: labels.get(value, "请选择订单"), key="order_picker")
            st.button("选入请求", disabled=selected is None, on_click=fill_order, args=(selected, single_order))
            st.caption("这里只查找并填入订单号，最终是否可申请以分析结果为准。")
        st.caption(f"共 {data['total']} 个订单 · 第 {page + 1} 页")
        left, right = st.columns(2)
        if left.button("上一页", disabled=page == 0, key="orders_previous"):
            st.session_state["order_page"] = page - 1
            st.rerun()
        if right.button("下一页", disabled=(page + 1) * 5 >= data["total"], key="orders_next"):
            st.session_state["order_page"] = page + 1
            st.rerun()


def analyze_form(current=None, *, missing=()):
    if current:
        label = "补充信息并分析" if current["kind"] == "clarification" else "重新分析"
    else:
        label = "开始分析"
    single_order = "single_order_id" in missing
    needs_order = single_order or "order_id" in missing
    lookup, entry = st.columns(2) if needs_order else (st.container(), st.container())
    with lookup:
        order_lookup(expanded=needs_order, single_order=single_order)
    original = current["source_question"] if current and current["status"] == "failed" else ""
    with entry:
        question = st.text_area("补充客户问题" if current else "客户问题", key="question", max_chars=4000,
                                value=original, placeholder="请输入客户原话，包括订单号和需要处理的问题。")
        if needs_order:
            st.caption("核对输入框内的订单号后再提交。提交前，补充信息仅保留在本页。")
        clicked = st.button(label, type="primary", key="analyze_submit")
    if clicked:
        if not question.strip():
            st.warning("请先填写客户问题或补充信息。")
            return
        if needs_order and len(set(re.findall(r"(?<![A-Za-z0-9])ORD-[A-Z0-9-]+", question.upper()))) != 1:
            entry.warning("请先选入或输入一笔明确的订单号。补充信息尚未保存。")
            return
        try:
            with st.spinner("正在查询订单与政策，生成处理方案…"):
                result = api.post("/proposals/analyze", {"message": question, "language": "zh",
                    "request_id": current["request_id"] if current else None})
                select_proposal(result)
        except APIError as exc:
            show_error(exc)
        st.rerun()


def request_list():
    if st.session_state.pop('clear_batch_on_render', False):
        clear_selection()
    render_request_list(api, select_proposal)


def close_confirmation():
    st.session_state.pop("ticket_confirmation", None)


@st.dialog("确认创建内部售后工单",
           **({"dismissible": False} if "dismissible" in signature(st.dialog).parameters else {}))
def confirm_ticket(snapshot, summary, order_id, requested_amount):
    st.write(f"订单 {order_id} · 申请金额 {requested_amount}")
    st.write(summary)
    st.info("本操作创建内部售后工单。退款与客户沟通需另行处理，回复草稿尚未发送。")
    cancel, submit = st.columns(2)
    if cancel.button("取消建单"):
        close_confirmation()
        st.rerun()
    if submit.button("最终确认建单", type="primary"):
        st.session_state["ticket_confirmation_submit"] = snapshot
        close_confirmation()
        st.rerun()


def request_detail():
    selected = api.get("/proposals/" + st.session_state["selected_proposal"])
    detail = api.get("/requests/" + selected["request_id"], params={"include_history": False})
    archived = detail["archived_at"] is not None
    current = detail["current"]
    select_proposal(current)
    identity = current["proposal_id"]
    if detail['status'] == 'unknown' and (current.get('error') or {}).get('code') == 'EXECUTION_UNKNOWN' and identity in st.session_state.get('known_not_executed', []):
        detail['status'], detail['status_label'] = 'reanalyze', '需重新分析'
    unknown = identity in st.session_state["unknown_execution"] or detail["status"] == "unknown"
    execution = None
    if current["status"] == "executed" and not unknown:
        try:
            execution = verify_execution(identity)
            if execution["status"] != "executed":
                raise APIError("EXECUTION_UNKNOWN", "结果待核验", uncertain=True)
        except APIError:
            st.session_state["unknown_execution"] = list(set(st.session_state["unknown_execution"] + [identity]))
            unknown = True
    st.subheader("请求详情")
    st.caption("请求编号 " + detail["request_id"])
    if archived:
        st.info("此请求已归档，恢复后才能继续处理。")
    payload = current["payload"]
    order_id = current.get("order_id") or payload.get("order_id") or current["entities"].get("order_id")
    order, policy, eligibility = proposal_facts(payload, order_id)
    duplicate = bool(detail["ticket"] and not detail["own_ticket"])
    active = detail["status"] in ("pending", "needs_info", "rejected") and not unknown and not archived
    editable = active and bool(current["payload_hash"]) and not current["synthetic_fixture"]
    pending = editable and detail["status"] == "pending"
    can_create = (pending and not duplicate and bool(order) and bool(policy)
                  and current["kind"] == "return_request"
                  and current["status"] in ("pending_approval", "approved")
                  and payload.get("action") == "create_aftersales_ticket"
                  and payload.get("eligibility") == eligibility.get("eligibility") == "eligible")
    can_reply = pending and current["kind"] == "reply_only" and current["status"] == "pending_approval"
    missing = payload.get("missing_fields") or []
    action_label, next_step = detail["status_label"], {
        "analyzing": "等待分析完成。",
        "failed": "查看错误原因后重新分析。",
        "manual": "请人工核实相关事实，再决定是否重新分析。",
        "reanalyze": "重新分析后审阅当前方案。",
        "rejected": "可查看原方案，必要时重新分析。",
        "accepted": "查看已核验的内部售后工单。",
        "reply_confirmed": "可复制已确认的草稿，后续沟通由客服处理。",
    }.get(detail["status"], "请核对当前方案和关联记录。")
    if unknown:
        action_label, next_step = "待核验", "请先查询提交结果。"
    elif archived:
        next_step = "如需继续处理，请先恢复请求。"
    elif detail["status"] == "needs_info":
        next_step = "、".join(MISSING_FIELD_LABELS.get(field, "缺少必要信息") for field in missing) or "请补充信息。"
    elif can_create:
        action_label, next_step = "待确认建单", "请核对依据后创建内部售后工单。"
    elif can_reply:
        action_label, next_step = "待确认回复", "请核对并确认客户回复草稿。"
    if editable and st.session_state.get("draft_" + identity, current["draft_reply"]) != current["draft_reply"]:
        next_step = "请先保存草稿修改，再确认处理。"
    with st.container(border=True):
        st.markdown(f"**{action_label} · 下一步**")
        st.write(next_step)
        if can_create or can_reply:
            st.markdown("[前往审核](#review-draft)")
        if detail["status"] == "needs_info" and not (unknown or archived or duplicate):
            st.caption("补齐信息后重新分析。当前请求尚未创建内部工单。")
            analyze_form(current, missing=missing)

    st.markdown("**关键判断依据**")
    eligibility_status = eligibility.get("eligibility", "unknown")
    application_amount = current["kind"] == "return_request" and eligibility_status == "eligible"
    cents = payload.get("requested_amount_cents") if application_amount else order.get("total_cents")
    currency = payload.get("currency") if application_amount else order.get("currency")
    shown_amount = amount(cents, currency) if cents is not None and currency else "未知"
    cols = st.columns(3)
    cols[0].metric("订单", order_id or "待补充")
    cols[1].metric("资格", status_name(eligibility_status))
    cols[2].metric("申请金额" if application_amount else "订单金额（仅供参考）", shown_amount)
    st.write("订单状态（订单查询）", status_name(order.get("status")) if order else "未知")
    reason = payload.get("reason") or current["entities"].get("reason")
    questions = [item["text"] for item in detail["questions"]]
    st.write("申请原因（AI 提取，待人工核对）", reason_name(reason) if reason else "未提取")
    if reason_needs_review(reason, questions):
        st.warning("申请原因与客户原话可能不一致。请核对原话，必要时补充信息后重新分析。")
    codes = eligibility.get("reason_codes") or []
    if codes:
        st.write("资格依据（规则判断）", " ".join(error_message(code) for code in codes))
    elif eligibility_status == "eligible":
        st.write("资格依据（规则判断）", "订单状态、政策期限与申请原因均符合当前退货规则。")
    else:
        st.write("资格依据（规则判断）", "缺少可核验的资格结果。")
    window = policy.get("return_window_days")
    st.write("政策窗口（政策查询）", f"签收后 {window} 天内" if window is not None else "未知")
    st.write("申请截止", decision_deadline(order, eligibility, eligibility.get("deadline_at"), settings.app_timezone))
    st.markdown("**客户原话与补充记录**")
    for question in detail["questions"]:
        st.write(question["text"])
    st.markdown("**处理方案**")
    st.info(business_text(payload.get("summary", "分析尚未完成")))
    if detail["status"] == "failed" and current.get("error"):
        st.warning(error_message(current["error"]["code"]))
    if duplicate:
        st.info("该订单已有售后申请，不会重复创建。")
    if unknown:
        st.warning("提交结果尚未确认，请先查询提交结果，核验前不能再次创建工单。")
        if st.button("查询提交结果"):
            try:
                value = verify_execution(identity)
                notify("已核验原工单。" if value["status"] == "executed" else
                       "已核验：尚未创建工单，请重新分析后再确认。" if (current.get('error') or {}).get('code') == 'EXECUTION_UNKNOWN' else
                       "已核验：尚未创建工单，可确认方案后重新提交。")
            except APIError as exc:
                show_error(exc, identity)
            st.rerun()
    elif execution:
        st.success(f"已创建工单：{execution['ticket']['ticket_no']} · 申请已受理")
        st.caption("申请受理不代表退款完成或退货完成。")
    elif detail["status"] == "reply_confirmed":
        st.success("草稿已确认，尚未发送。")
    if detail["ticket"] and not unknown:
        st.button("查看工单", on_click=open_ticket, args=(detail["ticket"]["id"],))
    draft_key = "draft_" + identity
    chinese_draft = business_text(current["draft_reply"])
    marker = "draft_language_" + identity
    if chinese_draft != current["draft_reply"] and st.session_state.get(marker) != current["payload_hash"]:
        if draft_key in st.session_state:
            st.session_state[draft_key] = business_text(st.session_state[draft_key])
        st.session_state[marker] = current["payload_hash"]
    draft = current["draft_reply"]
    if current["status"] == "failed":
        st.caption("暂无可用客户回复草稿。")
    else:
        st.subheader("审核客户回复", anchor="review-draft")
        draft = st.text_area("回复草稿（尚未发送）", value=chinese_draft, key=draft_key, height=160, disabled=not editable)
        st.caption("选中回复内容，按 Ctrl+C 复制。")
        if editable and re.search(r"没有创建工单|尚未创建工单|未写入数据库|no ticket (?:has been|was) created", draft, re.IGNORECASE):
            st.warning("此草稿含内部处理说明。请编辑并保存新版后再用于客户沟通。")
    dirty = draft != current["draft_reply"]
    guard = {"expected_revision": current["revision"], "expected_payload_hash": current["payload_hash"]}
    if editable:
        if dirty:
            st.info("草稿已修改，请先保存修改，再确认处理。")
        if st.button("保存修改", disabled=not dirty):
            try:
                select_proposal(api.post(f"/proposals/{identity}/revisions", {**guard, "draft_reply": draft}))
            except APIError as exc:
                show_error(exc)
            st.rerun()
    snapshot = (identity, current["revision"], current["payload_hash"])
    queued = st.session_state.pop("ticket_confirmation_submit", None)
    opened = st.session_state.get("ticket_confirmation")
    if queued is not None:
        if not can_create or dirty or queued != snapshot:
            notify("当前方案已变化，请刷新并重新确认。", "warning")
        else:
            try:
                proposal_id, revision, payload_hash = queued
                result = api.post(f"/proposals/{proposal_id}/confirm-and-create",
                                  {"expected_revision": revision, "expected_payload_hash": payload_hash})
                if result.get("status") != "executed":
                    raise APIError("EXECUTION_UNKNOWN", "提交结果待核验", uncertain=True)
                verify_execution(identity)
            except APIError as exc:
                show_error(exc, identity)
        st.rerun()
    if opened is not None and (opened != snapshot or not can_create or dirty):
        close_confirmation()
        st.warning("当前方案已变化，请重新核对后确认。")
    if can_create:
        st.caption("创建内部售后工单。退款和客户回复需另行处理。")
        if st.button("确认并创建内部售后工单", type="primary", disabled=dirty):
            st.session_state["ticket_confirmation"] = snapshot
        if st.session_state.get("ticket_confirmation") == snapshot:
            confirm_ticket(snapshot, business_text(payload.get("summary", "")), order_id, shown_amount)
    elif can_reply:
        st.caption("仅保存客服确认的回复草稿，尚未发送。")
        if st.button("确认回复草稿（不建单）", type="primary", disabled=dirty):
            try:
                api.post(f"/proposals/{identity}/approve", guard)
            except APIError as exc:
                show_error(exc)
            st.rerun()
    if active and current["status"] == "pending_approval":
        with st.expander("暂不采用", expanded=False):
            with st.form("reject_" + identity):
                reason = st.text_input("不采用原因")
                if st.form_submit_button("暂不采用方案"):
                    try:
                        api.post(f"/proposals/{identity}/reject", {**guard, "rejection_reason": reason})
                    except APIError as exc:
                        show_error(exc)
                    st.rerun()
    if detail["status"] in ("failed", "reanalyze", "rejected", "manual") and not unknown and not duplicate and not archived:
        analyze_form(current)
    for item in payload.get("evidence", []):
        title = {"get_order": "订单与物流信息", "get_return_policy": "适用退货政策", "check_return_eligibility": "退货资格判断"}.get(item["name"], "查询依据")
        with st.expander(title, expanded=False):
            st.table([{"项目": key, "内容": value} for key, value in evidence_rows(item["name"], item["result"], settings.app_timezone).items()])
    render_management(api, detail, unknown)
    render_history(api, current)


def tickets_view():
    st.subheader("售后工单")
    identity = st.session_state["selected_ticket"]
    if identity:
        if st.button("返回工单列表"):
            st.session_state["selected_ticket"] = None
            st.rerun()
        ticket = api.get("/aftersales-tickets/" + identity)
        st.success("工单号：" + ticket["ticket_no"])
        st.table([{"项目": label, "内容": value} for label, value in {
            "订单号": ticket["order_id"], "状态": status_name(ticket["status"]),
            "申请原因": reason_name(ticket["reason"]), "申请金额": amount(ticket["requested_amount_cents"], ticket["currency"]),
            "创建时间": local_time(ticket["created_at"], settings.app_timezone)}.items()])
        st.info("此工单表示售后申请已受理，不代表退款或退货完成。")
        st.text_area("已确认的回复草稿（尚未发送）", value=business_text(ticket["approved_reply"]), disabled=True)
    else:
        page = st.session_state["ticket_page"]
        rows = api.get("/aftersales-tickets", params={"limit": 11, "offset": page * 10})["items"]
        if not rows:
            st.info("暂无售后工单。")
        for ticket in rows[:10]:
            with st.container(border=True):
                st.write(f"{ticket['ticket_no']} · {ticket['order_id']} · {status_name(ticket['status'])}")
                st.write(amount(ticket["requested_amount_cents"], ticket["currency"]))
                if st.button("查看工单", key="ticket_" + ticket["id"]):
                    st.session_state["selected_ticket"] = ticket["id"]
                    st.rerun()
        left, right = st.columns(2)
        if left.button("上一页", disabled=page == 0, key="tickets_previous"):
            st.session_state["ticket_page"] -= 1
            st.rerun()
        if right.button("下一页", disabled=len(rows) <= 10, key="tickets_next"):
            st.session_state["ticket_page"] += 1
            st.rerun()


with st.sidebar:
    workspace = st.radio("工作区", ["售后请求", "售后工单"], key="workspace")
    if workspace == "售后请求":
        if st.button("新建请求"):
            st.session_state["new_request"] = True
            st.session_state["selected_proposal"] = None
            st.session_state.pop("question", None)
            st.rerun()
        if st.button("返回请求列表"):
            st.session_state["selected_proposal"] = None
            st.session_state["new_request"] = False
            st.rerun()
if st.session_state.get("notice"):
    kind, message = st.session_state.pop("notice")
    getattr(st, kind)(message)
try:
    if workspace == "售后工单":
        tickets_view()
    elif st.session_state["new_request"]:
        st.subheader("新建售后请求")
        analyze_form()
    elif st.session_state["selected_proposal"]:
        request_detail()
    else:
        request_list()
except APIError as exc:
    st.warning(error_message(exc.code, uncertain=exc.uncertain))
