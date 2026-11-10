from datetime import datetime
import re
from zoneinfo import ZoneInfo
from src.domain.products import PRODUCT_NAMES
from src.domain.clock import to_local


STATUS_NAMES = {
    "analyzing": "分析中", "needs_info": "待补充信息", "pending_approval": "待审批",
    "approved": "已批准", "rejected": "已拒绝", "superseded": "已被新版本替代",
    "expired": "已过期", "executed": "已执行", "failed": "处理失败",
    "requested": "已受理", "processing": "处理中", "closed": "已关闭", "cancelled": "已取消",
    "confirmed": "已确认", "shipped": "已发货", "delivered": "已签收", "in_transit": "运输中",
    "eligible": "符合申请条件", "ineligible": "不符合申请条件", "unknown": "待核实",
}
REASON_NAMES = {
    "defective": "商品故障", "wrong_item": "收到错误商品", "not_as_described": "商品与描述不符",
    "changed_mind": "不再需要", "damaged": "商品破损", "too_late": "送达过晚",
}
EVENT_NAMES = {
    'request_archived': '归档请求', 'request_restored': '恢复请求', 'request_purged': '永久清除请求',
    "analysis_started": "开始分析", "analysis_completed": "分析完成", "analysis_failed": "分析失败",
    "proposal_created": "保存建议", "proposal_superseded": "生成新版本", "proposal_approved": "批准建议",
    "proposal_rejected": "拒绝建议", "proposal_expired": "建议过期", "ticket_created": "创建售后工单",
    "execution_failed": "提交失败", "execution_unknown": "提交结果待核验", "fixture_ticket_seeded": "录入已有工单",
}
ERROR_MESSAGES = {
    'MODEL_OUTPUT_TRUNCATED': '模型回复未完整生成，分析未完成。请重新分析。',
    'REQUEST_ARCHIVED': '此请求已归档，请先恢复后再处理。',
    'STALE_MANAGEMENT': '请求已发生变化，请刷新后重新选择操作。',
    'MANAGEMENT_STATE': '归档状态已变化，请刷新后重试。',
    'ARCHIVE_REQUIRED': '请先归档此请求，再查看永久清除说明。',
    'REQUEST_HAS_TICKETS': '此请求有关联工单，不能永久清除；已关闭或取消的工单也需保留。',
    'CROSS_REQUEST_REFERENCE': '此请求与其他记录有关联，不能永久清除。',
    "OK": "读取成功", "ORDER_NOT_FOUND": "未找到订单，请核实订单号。",
    "ORDER_CANCELLED": "订单已取消，不能申请退货。", "ORDER_NOT_DELIVERED": "订单尚未签收，当前不能申请退货。",
    "DELIVERY_TIME_UNKNOWN": "签收时间缺失，请人工核实。", "DELIVERY_TIME_INVALID": "签收时间异常，请人工核实。",
    "POLICY_NOT_FOUND": "未找到适用的退货政策。", "POLICY_DISABLED": "适用的退货政策已停用。",
    "POLICY_RULES_INVALID": "退货政策信息异常，请人工核实。", "RETURN_ALREADY_EXISTS": "该订单已有处理中或已受理的申请，请查看原工单。",
    "RETURN_WINDOW_EXPIRED": "已超过退货申请期限。", "REASON_NOT_ALLOWED": "该退货原因不适用于当前政策。",
    "RETURN_INELIGIBLE": "当前订单不符合退货申请条件。", "DATABASE_NOT_READY": "数据服务尚未就绪，请稍后重新连接。",
    "DATABASE_UNAVAILABLE": "数据服务暂时不可用，请稍后重新连接。", "CONFIGURATION_ERROR": "服务配置或订单信息异常，请联系管理员。",
    "INVALID_ARGUMENT": "查询信息不完整，请核对订单号和申请原因。", "VALIDATION_ERROR": "提交内容不符合要求，请检查输入。",
    "PROPOSAL_NOT_FOUND": "未找到该建议，请重新选择记录。", "REQUEST_NOT_FOUND": "未找到原请求，请重新选择记录。",
    "TICKET_NOT_FOUND": "未找到该售后工单，请刷新记录。", "STALE_REVISION": "当前版本已变更，请查看最新版本后重新审核。",
    "PAYLOAD_TAMPERED": "建议内容校验失败，请重新分析并审核。", "PROPOSAL_EXPIRED": "建议已过期，请重新分析并审核。",
    "STALE_PROPOSAL": "订单或政策已变更，请重新分析并审核。", "APPROVAL_REQUIRED": "请先批准当前版本，再创建售后工单。",
    "APPROVAL_MISMATCH": "当前内容与已批准版本不一致，请重新审核。", "NOT_EXECUTABLE": "该建议只用于回复或人工处理，不能创建工单。",
    "EXECUTION_INCONSISTENT": "执行记录与工单不一致，请核查原请求。", "EXECUTION_CONFLICT": "该订单的申请记录已变更，请刷新并查看原工单。",
    "EXECUTION_FAILED": "工单提交失败，业务变更已回滚。", "EXECUTION_UNKNOWN": "提交结果尚未确认，请先查询当前建议的执行结果。",
    "REQUEST_UNKNOWN": "未收到提交响应，请先核验原记录。", "API_UNAVAILABLE": "服务暂时不可用，请重新连接。",
    "API_ERROR": "操作未完成，请检查当前记录。", "RESPONSE_INVALID": "响应无法核验，请查询原记录。",
    "VERIFICATION_MISMATCH": "工单回读结果不一致，请核查原请求。", "INTERNAL_ERROR": "服务暂时无法完成操作，请稍后查看原记录。",
    "UNAUTHORIZED": "身份验证失败，请联系管理员。", "OPERATOR_NOT_CONFIGURED": "客服身份尚未配置，请联系管理员。",
    "REQUEST_CONFLICT": "同一请求已生成新版本，请刷新后查看。", "REQUEST_BUSY": "该请求正在分析，请等待完成。",
    "REQUEST_EXECUTED": "该请求已经执行，请查看原工单。", "ANALYSIS_INTERRUPTED": "分析已中断，请重新分析。",
    "ANALYSIS_TIMEOUT": "分析超时，请查看当前记录后重新分析。", "REVISION_NOT_ALLOWED": "当前状态不能修改，请查看最新记录。",
    "REVIEW_ALREADY_DECIDED": "该版本已有审核决定，请查看原记录。", "REVIEW_NOT_ALLOWED": "只有当前待审批版本可以审核。",
    "MODEL_UNAVAILABLE": "模型服务暂时不可用，分析未完成。", "MODEL_TIMEOUT": "模型响应超时，分析未完成。",
    "MODEL_SCHEMA_INVALID": "模型返回内容无法核验，请重新分析。", "LLM_BUDGET_EXHAUSTED": "本次分析次数已达上限，请重新分析。",
    "TOOL_BUDGET_EXHAUSTED": "本次查询次数已达上限，请人工核实。", "TOOL_CALL_INVALID": "查询记录无法核验，请重新分析。",
    "UNKNOWN_TOOL": "分析请求了未支持的操作，已阻止执行。", "READ_TIMEOUT": "事实查询超时，请稍后重新分析。",
    "READ_FAILED": "事实查询失败，请稍后重新分析。", "MCP_UNAVAILABLE": "事实查询服务暂时不可用，请稍后重新分析。",
    "MCP_PROTOCOL_ERROR": "事实查询响应异常，请联系管理员。", "MCP_TOOLSET_INVALID": "事实查询服务配置异常，请联系管理员。",
    "EVIDENCE_MISSING": "建议缺少必要的事实依据，请重新分析。", "EVIDENCE_INVALID": "事实依据无法核验，请重新分析。",
    "EVIDENCE_INCONSISTENT": "建议与查询事实不一致，请重新分析。", "INELIGIBLE_ACTION": "当前事实不支持创建退货工单。",
}


def status_name(value):
    return STATUS_NAMES.get(value, "未知状态")


def reason_name(value):
    return REASON_NAMES.get(value, "未提供" if not value else "其他原因")


def error_message(code, *, uncertain=False):
    return ERROR_MESSAGES.get(code, "提交结果尚未确认，请先查询当前建议的执行结果。" if uncertain else "操作未完成，请查看当前记录。")


def business_text(value):
    names = {**STATUS_NAMES, **REASON_NAMES, "demo-return": "整单退货", "return_request": "退货申请",
             "reply_only": "仅回复", "handoff": "人工处理", "clarification": "补充信息",
             "whole_order": "整单", "create_aftersales_ticket": "创建售后工单", "USD": "美元",
             "More information needed": "需要补充信息", "Analysis failed": "分析未完成"}
    expression = r"(?<![A-Za-z0-9_-])(" + "|".join(re.escape(key) for key in sorted(names, key=len, reverse=True)) + r")(?![A-Za-z0-9_-])"
    return re.sub(expression, lambda match: names[match.group()], value)


def local_time(value, timezone="local"):
    if not value:
        return "未知"
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone == "local":
        local, place = to_local(instant), "本地时间"
    else:
        local = instant.astimezone(ZoneInfo(timezone))
        place = {"Australia/Sydney": "悉尼时间", "Asia/Shanghai": "北京时间", "UTC": "协调世界时"}.get(timezone, "当地时间")
    return local.strftime("%Y-%m-%d %H:%M:%S") + f"（{place}）"


def amount(cents, currency):
    if not isinstance(cents, int):
        return "未知"
    unit = {"USD": "美元", "CNY": "人民币", "AUD": "澳元", "EUR": "欧元", "GBP": "英镑", "JPY": "日元", "HKD": "港元"}.get(currency, currency or "金额")
    return f"{cents // 100}.{cents % 100:02d} {unit}"


def evidence_rows(name, result, timezone):
    data = result.get("data") or {}
    if not result.get("ok") or data.get("found") is False:
        return {"查询结果": error_message(result.get("code"))}
    date = lambda value: local_time(value, timezone)
    if name == "get_order":
        rows = {"订单号": data.get("order_id", "未知"), "订单状态": status_name(data.get("status")),
                "订单金额": amount(data.get("total_cents"), data.get("currency")), "签收时间": date(data.get("delivered_at"))}
        products = [PRODUCT_NAMES.get(item.get("product_name"), item.get("product_name", "商品")) for item in data.get("items", [])]
        rows["商品"] = "、".join(products) or "未知"
        shipment = data.get("shipment") or {}
        if shipment:
            rows.update({"运单号": shipment.get("tracking_number", "未知"), "物流状态": status_name(shipment.get("status")),
                         "预计送达日期": shipment.get("estimated_delivery", "未知")})
        return rows
    if name == "get_return_policy":
        return {"政策编号": data.get("policy_id", "未知"), "政策版本": f"第 {data.get('version', '未知')} 版",
                "申请期限": f"签收后 {data.get('return_window_days', '未知')} 天，包含截止时刻",
                "允许原因": "、".join(reason_name(value) for value in data.get("allowed_reasons", [])),
                "适用范围": "整单", "政策状态": "启用" if data.get("enabled") else "停用"}
    if name == "check_return_eligibility":
        rows = {"订单号": data.get("order_id", "未知"), "申请资格": status_name(data.get("eligibility")),
                "判断依据": "；".join(error_message(code) for code in data.get("reason_codes", [])) or "符合当前退货政策",
                "判断时间": date(data.get("evaluated_at")), "申请截止": date(data.get("deadline_at")),
                "申请金额": amount(data.get("requested_amount_cents"), data.get("currency"))}
        ticket = data.get("existing_ticket")
        if ticket:
            rows.update({"原工单号": ticket.get("ticket_no", "未知"), "原工单状态": status_name(ticket.get("status"))})
        return rows
    return {"查询结果": "该查询无法作为业务依据，请重新分析。"}
