from datetime import datetime, time, timedelta

import streamlit as st

from src.config import settings
from src.services.request_service import STATUS_NAMES
from src.ui.api_client import APIError
from src.ui.presentation import EVENT_NAMES, business_text, error_message, local_time, status_name


def clear_selection():
    for key in list(st.session_state):
        if key.startswith('batch_request_'):
            del st.session_state[key]


def render_request_list(api, select_proposal):
    st.subheader('售后请求')
    archive_label = st.selectbox('请求范围', ['处理中', '已归档', '全部'], key='archive_filter')
    scope = {'处理中': 'active', '已归档': 'archived', '全部': 'all'}[archive_label]
    status = st.selectbox('处理状态', [''] + list(STATUS_NAMES), key='status_filter',
                         format_func=lambda value: STATUS_NAMES.get(value, '全部'))
    query = st.text_input('搜索问题或订单号', key='request_search')
    st.caption('列表显示已保存的处理进度；进入详情后核验工单结果。归档只收起请求，不代表业务完成。')
    dates = None
    if st.checkbox('按更新时间筛选', key='filter_dates'):
        dates = st.date_input('更新时间范围（本机日期，含起止日）', value=(datetime.now().date(), datetime.now().date()), key='request_dates')
        if not isinstance(dates, tuple) or len(dates) != 2:
            st.info('请选择完整的起止日期。')
            return
    filters = (scope, status, query, dates)
    if st.session_state.get('request_filters') != filters:
        clear_selection()
        st.session_state['request_cursors'] = [None]
        st.session_state['request_filters'] = filters
    cursors = st.session_state.setdefault('request_cursors', [None])
    params = dict(q=query, limit=10, archived=scope, include_total=False)
    if status:
        params['status'] = status
    if cursors[-1]:
        params['cursor'] = cursors[-1]
    if dates:
        params['updated_from'] = datetime.combine(dates[0], time.min).astimezone().isoformat()
        params['updated_to'] = datetime.combine(dates[1] + timedelta(days=1), time.min).astimezone().isoformat()
    data = api.get('/requests', params=params)
    rows = data['items']
    viewed = st.session_state.get('management_list_view', {})
    st.session_state['management_list_view'] = {row['request_id']: row['management_token'] for row in rows}
    batch = st.checkbox('批量管理', key='batch_manage')
    if not batch:
        clear_selection()
    if batch and rows and st.button('选择当前页'):
        for row in rows:
            st.session_state['batch_request_' + row['request_id']] = True
    if not rows:
        st.info('暂无符合条件的请求。点击“新建请求”开始处理。')
    selected = []
    for item in rows:
        with st.container(border=True):
            if batch and st.checkbox('选择此请求', key='batch_request_' + item['request_id']):
                selected.append(item)
            st.write(f"{item['order_id'] or '待补充订单'} · {item['status_label']}" + (' · 已归档' if item['archived_at'] else ''))
            st.write(item['question'][:140])
            st.caption(local_time(item['updated_at'], settings.app_timezone))
            if st.button('查看请求', key='request_' + item['request_id']):
                select_proposal(item)
                st.rerun()
    if batch:
        st.caption(f'已选择当前页 {len(selected)} 条；整批成功或整批撤销。')
        left, right = st.columns(2)
        for column, action, label in ((left, 'archive', '归档所选'), (right, 'restore', '恢复所选')):
            disabled = not selected or any(bool(row['archived_at']) == (action == 'archive') for row in selected)
            if column.button(label, disabled=disabled):
                try:
                    api.post('/requests/batch-' + action, {'items': [dict(request_id=row['request_id'],
                        management_token=viewed.get(row['request_id'], row['management_token'])) for row in selected]})
                    st.session_state['notice'] = ('success', '所选请求已归档。' if action == 'archive' else '所选请求已恢复。')
                    st.session_state['request_cursors'] = [None]
                except APIError as exc:
                    st.session_state['notice'] = ('warning', error_message(exc.code))
                # Clear on next render before these checkboxes are instantiated.
                st.session_state['request_filters'] = None
                st.rerun()
    st.caption(f'第 {len(cursors)} 页')
    left, right = st.columns(2)
    if left.button('上一页', disabled=len(cursors) == 1, key='requests_previous'):
        st.session_state['request_cursors'] = cursors[:-1]
        st.session_state['clear_batch_on_render'] = True
        st.rerun()
    if right.button('下一页', disabled=not data['has_more'], key='requests_next'):
        st.session_state['request_cursors'] = cursors + [data['next_cursor']]
        st.session_state['clear_batch_on_render'] = True
        st.rerun()


def render_management(api, detail, unknown):
    identity = detail['request_id']
    token = detail['management_token']
    view_key = 'management_view_' + identity
    viewed_token = st.session_state.get(view_key, token)
    st.session_state[view_key] = token
    archived = detail['archived_at'] is not None
    with st.expander('请求整理', expanded=False):
        st.caption('归档只收起请求，不取消工单。恢复后才能补充、修改或确认。')
        action, label = ('restore', '恢复请求') if archived else ('archive', '归档请求')
        if st.button(label, disabled=unknown or detail['status'] == 'analyzing'):
            try:
                api.post(f'/requests/{identity}/{action}', {'management_token': viewed_token})
                st.session_state['notice'] = ('success', '请求已归档。' if action == 'archive' else '请求已恢复。')
                st.session_state.pop('purge_preview', None)
            except APIError as exc:
                st.session_state['notice'] = ('warning', error_message(exc.code))
            st.rerun()
        if not archived:
            return
        st.warning('永久清除无法恢复，只允许清除所有版本均无关联工单的请求。')
        if st.button('查看永久清除说明', disabled=unknown):
            st.session_state.pop('purge_confirmation', None)
            try:
                st.session_state['purge_preview'] = api.get(f'/requests/{identity}/purge-preview')
            except APIError as exc:
                st.warning(error_message(exc.code))
        preview = st.session_state.get('purge_preview')
        if not preview or preview['request_id'] != identity or preview['management_token'] != token:
            return
        st.write('将永久清除：' + preview['question'][:200])
        st.write(f"共 {preview['version_count']} 个版本及其历史操作记录，无法恢复。")
        reason = st.selectbox('固定清理原因', ['test_data', 'duplicate_request', 'no_longer_needed'],
            format_func=lambda value: {'test_data': '测试数据', 'duplicate_request': '重复请求', 'no_longer_needed': '不再需要保留'}[value])
        confirmation = st.text_input('请输入“永久清除”确认', key='purge_confirmation')
        if st.button('永久清除此请求', disabled=unknown or confirmation != '永久清除'):
            try:
                api.post(f'/requests/{identity}/purge', dict(management_token=token, reason=reason, confirmation=confirmation))
                st.session_state['selected_proposal'] = None
                st.session_state['request_filters'] = None
                st.session_state.pop('purge_preview', None)
                st.session_state.pop('purge_confirmation', None)
                st.session_state['notice'] = ('success', '请求已永久清除。')
            except APIError as exc:
                st.session_state['notice'] = ('warning', error_message(exc.code))
            st.rerun()


def render_history(api, current):
    identity = current['request_id']
    with st.expander('历史版本', expanded=False):
        key = 'history_' + identity
        if st.button('加载历史版本'):
            st.session_state[key] = api.get(f'/requests/{identity}/history', params={'limit': 10})
        data = st.session_state.get(key)
        if data:
            for row in data['items']:
                st.caption(f"第 {row['revision']} 版 · {status_name(row['status'])} · {local_time(row['created_at'], settings.app_timezone)}")
                if st.button('查看此版本', key='history_open_' + row['proposal_id']):
                    st.session_state['history_content_' + identity] = api.get('/proposals/' + row['proposal_id'])
            if data['next_revision'] and st.button('更早的版本'):
                st.session_state[key] = api.get(f'/requests/{identity}/history', params={'limit': 10, 'before_revision': data['next_revision']})
                st.rerun()
        content = st.session_state.get('history_content_' + identity)
        if content:
            st.caption(f"第 {content['revision']} 版，仅供查阅")
            st.write(content['source_question'])
            st.write(business_text(content['payload'].get('summary', '分析未完成')))
            st.write(business_text(content['draft_reply']))
    with st.expander('操作记录', expanded=False):
        key = 'audit_' + identity
        if st.button('加载操作记录'):
            st.session_state['audit_offset_' + identity] = 0
            st.session_state[key] = api.get('/audit-logs', params={'request_id': identity, 'limit': 11})['items']
        logs = st.session_state.get(key, [])
        if logs:
            st.table([{'时间': local_time(item['created_at'], settings.app_timezone),
                '操作': EVENT_NAMES.get(item['event_type'], '其他操作'), '操作者': '系统' if item['actor_id'] in ('system', 'demo-seed') else
                '当前客服' if item['actor_id'] == settings.operator_id else item['actor_id']} for item in logs[:10]])
            if len(logs) > 10 and st.button('更早的操作记录'):
                offset = st.session_state.get('audit_offset_' + identity, 0) + 10
                st.session_state['audit_offset_' + identity] = offset
                st.session_state[key] = api.get('/audit-logs', params={'request_id': identity, 'limit': 11, 'offset': offset})['items']
                st.rerun()
