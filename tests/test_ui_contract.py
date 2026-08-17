import re
from pathlib import Path


def frontend_js() -> str:
    paths = sorted([*Path('frontend').glob('*.js'), *Path('frontend').glob('*.mjs')])
    return ''.join(path.read_text(encoding='utf-8') for path in paths)


def customer_text() -> str:
    return Path('frontend/index.html').read_text(encoding='utf-8') + frontend_js()


def test_customer_page_has_no_competitor_or_internal_model_names():
    text = customer_text()
    banned = ['DeepSeek', 'Claude', 'OpenAI', 'Qwen', 'InternVL', '特征工程', '权重', '梯度', 'logistic', '算法模型']
    assert all(word.lower() not in text.lower() for word in banned)


def test_customer_page_exposes_complete_product_workflow():
    text = Path('frontend/index.html').read_text(encoding='utf-8')
    for word in [
        '图片', '视频', '音频', '文档', '批量导入', '待复核', '待更新', '无法判断',
        '负责人', '业务优先级', '更新资料', '导出报告', '复制链接', '数据留存', '工作空间',
    ]:
        assert word in text


def test_first_release_has_no_product_version_aliases():
    paths = [
        *Path('guanchao').glob('*.py'),
        Path('frontend/index.html'),
        *Path('frontend').glob('*.js'),
        *Path('frontend').glob('*.mjs'),
        Path('README.md'),
        Path('pyproject.toml'),
    ]
    pattern = re.compile(r'(?i)(^|[^a-z0-9])v[12]([^a-z0-9]|$)')
    for path in paths:
        assert not pattern.search(path.read_text(encoding='utf-8')), path


def test_switching_cases_discards_stale_polling_and_case_detail_results():
    core = Path('frontend/app-core.js').read_text(encoding='utf-8')
    runtime = Path('frontend/runtime.mjs').read_text(encoding='utf-8')
    assert 'state.caseId && state.caseId !== id && state.polling' in core
    assert core.count('runId !== state.runId || caseId !== state.caseId') >= 2
    assert 'latestDetail' in runtime
    assert 'coordinateRead' in runtime
    assert '__stale_case_request__' in runtime
    assert 'caseSelection' in runtime
    assert 'onCaseState: selectCaseState' in runtime
    assert 'onRunState: observeRunState' in runtime


def test_review_queue_supports_continuous_keyboard_review_without_double_submit():
    text = customer_text()
    for decision in ['confirm_ordinary', 'uncertain', 'confirm_marketing']:
        assert decision in text
    for key in ["event.key === '1'", "event.key === '2'", "event.key === '3'"]:
        assert key in text
    runtime = Path('frontend/runtime.mjs').read_text(encoding='utf-8')
    assert 'reviewSubmitting' in runtime
    assert 'reviewBusy' in runtime


def test_batch_and_monitoring_are_real_api_workflows_not_static_copy():
    text = Path('frontend/app-core.js').read_text(encoding='utf-8')
    for endpoint in ['/api/cases/batch', '/api/monitoring?due_only=true', '/target', '/report?output=markdown', '/api/members', '/api/workspace/settings']:
        assert endpoint in text


def test_browser_cannot_choose_or_forge_workspace_identity():
    js = frontend_js()
    html = Path('frontend/index.html').read_text(encoding='utf-8')
    assert '/api/session' in js
    assert 'X-Guanchao-Actor' not in js
    assert 'localStorage' not in js
    assert 'actorSelect' not in js + html
    assert '当前身份' in html and '浏览器不能自行切换' in html


def test_customer_workspace_has_collaborative_notes_separate_from_agent_chat():
    html = Path('frontend/index.html').read_text(encoding='utf-8')
    js = Path('frontend/app-core.js').read_text(encoding='utf-8')
    runtime = Path('frontend/runtime.mjs').read_text(encoding='utf-8')
    assert '协作备注' in html
    assert '/comments' in js
    assert 'caseNoteInput' in js
    assert "meta.url.pathname.endsWith('/comments')" not in runtime


def test_runtime_protects_drafts_stale_writes_and_modal_keyboard_access():
    runtime = Path('frontend/runtime.mjs').read_text(encoding='utf-8')
    for token in ['restoreDraft', 'staleWriteResponse', 'isSensitiveCaseWrite', "event.key !== 'Escape'", '避免素材漏出本轮证据']:
        assert token in runtime
    backend = Path('guanchao/api.py').read_text(encoding='utf-8')
    assert '证据快照' in backend


def test_asset_management_and_dialog_accessibility_are_real_interactions():
    text = Path('frontend/interaction.mjs').read_text(encoding='utf-8')
    for token in ['assetDeletePath', "method: 'DELETE'", 'aria-modal', "event.key !== 'Tab'", 'shell.inert', 'aria-selected']:
        assert token in text
    bootstrap = Path('frontend/app.js').read_text(encoding='utf-8')
    assert 'installInteractionEnhancements' in bootstrap
