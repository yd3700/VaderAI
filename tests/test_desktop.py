"""Desktop integration uses the real runner and temporary synthetic documents."""
import threading
import time

import pytest

from vader.core import config, seal
from vader.core.checkpoint import CheckpointStore
from vader.core.installer import InstallError
from vader.core.loader import LoadedRune
from vader.core.runner import JobRunner
from vader.desktop.controller import DesktopController
from vader.desktop.host import DesktopAPI
from vader.sdk import RuneField, RuneManifest, RuneResult, UserActionRequest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = tmp_path / 'settings.json'
    settings.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(config, 'SETTINGS_PATH', settings)
    config.invalidate()
    monkeypatch.setattr(config, 'data_dir', lambda: tmp_path)
    monkeypatch.setattr(config, 'rune_dir', lambda: tmp_path / 'runes')
    path = tmp_path / 'synthetic.txt'
    path.write_text('synthetic input', encoding='utf-8')

    def make(fn, fields=()):
        class Rune:
            manifest = RuneManifest('desktop_test', '테스트 룬', required_fields=list(fields))

            def run(self, ctx, inputs):
                return fn(ctx, inputs)
        rune = Rune()
        runner = JobRunner(CheckpointStore(tmp_path / 'checkpoints'), data_dir=tmp_path)
        return DesktopController(runner, loaded=[LoadedRune(rune.manifest, rune)])

    yield make, str(path)
    config.invalidate()


def wait_request(controller):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        jobs = controller.poll()
        if jobs and jobs[0]['request']:
            return jobs[0]['request']
        time.sleep(.01)
    pytest.fail('No user request arrived')


def test_real_job_round_trip_with_correction_and_artifact(setup):
    make, path = setup
    seen = {}

    def run(ctx, inputs):
        seen['dry_run'] = ctx.dry_run
        seen['input'] = inputs[0].path
        ctx.report_progress(1, 2, '문서 확인')
        answer = ctx.request_user_action(UserActionRequest('correct_value', '코드를 확인하세요.'))
        output = ctx.workdir / 'result.txt'
        output.write_text(answer.value, encoding='utf-8')
        ctx.report_progress(2, 2, '완료')
        return RuneResult(True, artifacts=[output], summary={'수정 코드': answer.value})

    controller = make(run)
    job = controller.submit('desktop_test', [path])
    request = wait_request(controller)
    with pytest.raises(ValueError, match='만료'):
        controller.respond(job['id'], 'old-request', 'corrected', 'WRONG')
    with pytest.raises(ValueError, match='입력'):
        controller.respond(job['id'], request['id'], 'corrected', '')
    controller.respond(job['id'], request['id'], 'corrected', 'A123')
    assert controller.runner.join(job['id'], 5)
    result = controller.poll()[0]
    assert result['state'] == 'succeeded'
    assert result['current'] == result['total'] == 2
    assert result['request'] is None
    assert result['summary'] == {'수정 코드': 'A123'}
    assert controller.artifact(job['id'], 0).read_text() == 'A123'
    assert seen['dry_run'] is True
    assert str(seen['input']) == path
    with pytest.raises(ValueError):
        controller.artifact(job['id'], -1)


def test_cancel_waiting_job_and_reject_concurrent_submit(setup):
    make, path = setup
    controller = make(lambda ctx, _: ctx.request_user_action(UserActionRequest('confirm', '계속?')))
    job = controller.submit('desktop_test', [path])
    wait_request(controller)
    assert controller.busy()
    with pytest.raises(ValueError, match='진행 중'):
        controller.submit('desktop_test', [path])
    assert controller.cancel(job['id'])
    assert controller.runner.join(job['id'], 5)
    assert controller.poll()[0]['state'] == 'cancelled'
    assert not controller.busy()


def test_settings_validate_and_preserve_secret(setup):
    make, path = setup
    controller = make(lambda *_: RuneResult(True), [RuneField('count', 'number'), RuneField('token', 'secret')])
    with pytest.raises(ValueError, match='설정'):
        controller.submit('desktop_test', [path])
    controller.save_settings('desktop_test', {'count': '3', 'token': 'private-token'})
    public = controller.catalog()['runes'][0]['fields']
    assert public[1]['value'] == '' and public[1]['configured']
    controller.save_settings('desktop_test', {'count': '4', 'token': ''})
    assert config.rune_config('desktop_test') == {'count': 4, 'token': 'private-token'}


def test_input_validation_and_bridge_allowlist(setup):
    make, path = setup
    controller = make(lambda *_: RuneResult(True))
    with pytest.raises(ValueError):
        controller.submit('desktop_test', [])
    with pytest.raises(FileNotFoundError):
        controller.submit('desktop_test', [path + '.missing'])
    with pytest.raises(ValueError):
        controller.submit('desktop_test', [path], dry_run='false')
    api = DesktopAPI(controller)
    assert not api.call('__getattribute__', {'name': '_controller'})['ok']
    assert api.call('catalog')['ok']


def test_runner_failure_is_not_a_fake_success(setup):
    make, path = setup

    def fail(*_):
        raise RuntimeError('테스트 오류')

    controller = make(fail)
    job = controller.submit('desktop_test', [path])
    assert controller.runner.join(job['id'], 5)
    result = controller.poll()[0]
    assert result['state'] == 'failed'
    assert result['failure']['message'] == '테스트 오류'
    assert result['artifacts'] == []


def test_parallel_submit_accepts_only_one_job(setup):
    make, path = setup
    release = threading.Event()
    controller = make(lambda *_: (release.wait(5), RuneResult(True))[1])
    results = []

    def submit():
        try:
            results.append(controller.submit('desktop_test', [path]))
        except ValueError:
            results.append(None)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    release.set()
    assert sum(r is not None for r in results) == 1
    assert controller.runner.join(controller.active, 5)


def test_close_gate_prevents_a_late_submit(setup):
    make, path = setup
    controller = make(lambda *_: RuneResult(True))
    assert controller.begin_close()
    with pytest.raises(ValueError, match='종료'):
        controller.submit('desktop_test', [path])


def test_bridge_preserves_installed_rune_seal_gate(setup):
    make, path = setup
    controller = make(lambda *_: RuneResult(True))
    (config.rune_dir() / 'desktop_test').mkdir(parents=True)
    seal.trust('desktop_test', config.rune_dir() / 'desktop_test')
    assert controller.catalog()['runes'][0]['locked']
    with pytest.raises(RuntimeError):
        controller.submit('desktop_test', [path], dry_run=False)
    job = controller.submit('desktop_test', [path], dry_run=True)
    assert controller.runner.join(job['id'], 5)


def test_choice_response_rejects_values_outside_options(setup):
    make, path = setup

    def run(ctx, _):
        answer = ctx.request_user_action(UserActionRequest('choice', '선택', ['A', 'B']))
        return RuneResult(True, summary={'선택': answer.value})

    controller = make(run)
    job = controller.submit('desktop_test', [path])
    request = wait_request(controller)
    with pytest.raises(ValueError, match='목록'):
        controller.respond(job['id'], request['id'], 'ok', 'C')
    assert not controller.begin_close()
    controller.respond(job['id'], request['id'], 'ok', 'B')
    assert controller.runner.join(job['id'], 5)
    assert controller.poll()[0]['summary'] == {'선택': 'B'}


def _install(controller, chosen):
    """화면과 같은 길: 고르면 요약을 보이고, 사람이 그 지문으로 설치를 누른다."""
    shown = controller.preview(str(chosen))
    return controller.install(shown['path'], shown['fingerprint'])


def test_install_refuses_a_rune_changed_after_the_person_saw_it(setup, tmp_path):
    make, _ = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    source = tmp_path / 'src' / 'desktop_swapped'
    source.mkdir(parents=True)
    body = ('from vader.sdk import RuneManifest, RuneResult\n'
            'import shutil\n'
            'class R:\n'
            '    manifest = RuneManifest("desktop_swapped", "바꿔치기")\n'
            '    def run(self, ctx, inputs):\n'
            '        shutil.rmtree(ctx.workdir)\n'
            '        return RuneResult(ok=True)\n'
            'rune = R()\n')
    (source / '__init__.py').write_text(body, encoding='utf-8')
    shown = controller.preview(str(source))
    assert shown['id'] == 'desktop_swapped' and shown['modules'] == ['shutil']
    assert shown['watch'] == ['폴더 삭제 · __init__.py 6줄']

    (source / '__init__.py').write_text(body + '# 몰래\n', encoding='utf-8')
    with pytest.raises(InstallError, match='확인한 뒤에'):
        controller.install(shown['path'], shown['fingerprint'])
    assert 'desktop_swapped' not in controller.runes


def test_install_from_the_runes_page_reloads_the_catalog(setup, tmp_path):
    make, _ = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    source = tmp_path / 'src' / 'desktop_installed'
    source.mkdir(parents=True)
    (source / '__init__.py').write_text(
        'from vader.sdk import RuneField, RuneManifest, RuneResult\n'
        'class R:\n'
        '    manifest = RuneManifest("desktop_installed", "설치 시험", required_fields=[RuneField("x")])\n'
        '    def run(self, ctx, inputs):\n'
        '        return RuneResult(ok=True)\n'
        'rune = R()\n', encoding='utf-8')
    data = _install(controller, source / '__init__.py')   # 고르기 창 하나: 폴더 안 파일을 고르면 그 폴더

    assert data['installed'] == {'id': 'desktop_installed', 'name': '설치 시험', 'version': '1.0.0', 'change': 'new',
                                 'granted': [], 'missing': ['x']}
    assert 'desktop_installed' in [r['id'] for r in data['runes']]


def test_seal_then_uninstall_from_the_runes_page(setup, tmp_path):
    """관리 화면에서 옮겨 온 봉인·제거. 봉인하면 실사용이 열리고, 제거하면 목록에서 빠진다."""
    make, path = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    source = tmp_path / 'src' / 'desktop_sealed'
    source.mkdir(parents=True)
    (source / '__init__.py').write_text(
        'from vader.sdk import RuneManifest, RuneResult\n'
        'class R:\n'
        '    manifest = RuneManifest("desktop_sealed", "봉인 시험")\n'
        '    def run(self, ctx, inputs):\n'
        '        return RuneResult(ok=True)\n'
        'rune = R()\n', encoding='utf-8')
    _install(controller, source)
    locked = {r['id']: r['locked'] for r in controller.catalog()['runes']}
    assert locked['desktop_sealed'] is True
    with pytest.raises(RuntimeError):   # 봉인 전에는 러너가 실사용 실행을 거부한다
        controller.runner.submit(controller.runes['desktop_sealed'].rune, [], dry_run=False)

    data = controller.seal('desktop_sealed', '연습 실행으로 확인')
    assert {r['id']: r['locked'] for r in data['runes']}['desktop_sealed'] is False

    data = controller.uninstall('desktop_sealed')
    assert 'desktop_sealed' not in [r['id'] for r in data['runes']]
    with pytest.raises(ValueError):
        controller.uninstall('desktop_sealed')


def test_mapping_maintenance_is_offered_only_by_runes_that_provide_it(setup):
    from types import SimpleNamespace
    make, _ = setup
    calls = []

    class Service:
        def review_items(self):
            return [SimpleNamespace(review='확인필요', insurance_code='K1', learning_key='거즈',
                                    current_code='', scope='insurance')]

        def learned_rows(self):
            return [{'vendor': '가나메디칼', 'item_name': '거즈', 'mapped_code': 'AA1'}]

        def correct(self, item, code):
            calls.append(('correct', item.learning_key, code))
            return item.scope

        def edit_learning(self, edits):
            calls.append(('edit', [(e['vendor'], e['item_name'], e['new_vendor'], e['new_item_name'], e['new_code'])
                                   for e in edits]))
            return len(edits)

        def delete_learning(self, vendor, name):
            calls.append(('delete', vendor, name))
            return True

    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    assert controller.catalog()['runes'][0]['mapping'] is False
    with pytest.raises(ValueError):
        controller.mapping('desktop_test')

    controller.runes['desktop_test'].rune.mapping_maintenance = lambda *_: Service()
    assert controller.catalog()['runes'][0]['mapping'] is True
    data = controller.mapping('desktop_test')
    assert data['review'][0]['name'] == '거즈' and data['learned'][0]['code'] == 'AA1'
    assert controller.mapping_correct('desktop_test', 0, 'BB2')['scope'] == 'insurance'
    with pytest.raises(ValueError):
        controller.mapping_correct('desktop_test', 5, 'BB2')   # 목록이 바뀌었으면 엉뚱한 행을 고치지 않는다
    saved = controller.mapping_save('desktop_test', [
        {'vendor': '가나메디칼', 'name': '거즈', 'new_vendor': '가나메디칼', 'new_name': '멸균거즈', 'new_code': 'CC3'}])
    assert saved['count'] == 1
    assert controller.mapping_delete('desktop_test', [{'vendor': '가나메디칼', 'name': '거즈'}])['count'] == 1
    with pytest.raises(ValueError):
        controller.mapping_save('desktop_test', [])
    assert calls == [('correct', '거즈', 'BB2'), ('edit', [('가나메디칼', '거즈', '가나메디칼', '멸균거즈', 'CC3')]),
                     ('delete', '가나메디칼', '거즈')]


def test_bridge_no_longer_switches_to_the_classic_screen(setup):
    make, _ = setup
    api = DesktopAPI(make(lambda ctx, inputs: RuneResult(ok=True)))
    assert not api.call('classic')['ok']
    assert api.call('ai_choice')['ok']


def _finish(controller, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = next(j for j in controller.poll() if j['id'] == job_id)
        if job['terminal']:
            return job
        time.sleep(.01)
    pytest.fail('job did not finish')


def test_promote_reruns_the_same_practice_input_for_real(setup):
    """결과 화면의 '실전 실행을 진행할까요?' — 연습한 그 파일·그 시작 페이지만 실전에 들어간다."""
    make, path = setup
    seen = []
    controller = make(lambda ctx, inputs: seen.append((ctx.dry_run, ctx.params['start_page'],
                                                       [i.path.name for i in inputs])) or RuneResult(ok=True))
    practice = controller.submit('desktop_test', [path], dry_run=True, start_page=3)
    assert _finish(controller, practice['id'])['state'] == 'succeeded'

    real = controller.promote(practice['id'])
    assert real['dry_run'] is False
    _finish(controller, real['id'])
    assert seen == [(True, 3, ['synthetic.txt']), (False, 3, ['synthetic.txt'])]
    assert next(j for j in controller.poll() if j['id'] == practice['id'])['promoted'] is True
    with pytest.raises(ValueError):          # 두 번 넘기지 않는다
        controller.promote(practice['id'])
    with pytest.raises(ValueError):          # 실전 실행은 다시 '실전으로' 넘길 대상이 아니다
        controller.promote(real['id'])


def test_promote_refuses_failed_practice_and_unapproved_rune(setup, tmp_path):
    make, path = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=False))
    failed = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, failed['id'])
    with pytest.raises(ValueError):
        controller.promote(failed['id'])

    source = tmp_path / 'src' / 'desktop_unapproved'
    source.mkdir(parents=True)
    (source / '__init__.py').write_text(
        'from vader.sdk import RuneManifest, RuneResult\n'
        'class R:\n'
        '    manifest = RuneManifest("desktop_unapproved", "승인 전")\n'
        '    def run(self, ctx, inputs):\n'
        '        return RuneResult(ok=True)\n'
        'rune = R()\n', encoding='utf-8')
    _install(controller, source)
    practice = controller.submit('desktop_unapproved', [path], dry_run=True)
    _finish(controller, practice['id'])
    with pytest.raises(ValueError, match='승인'):
        controller.promote(practice['id'])


def test_promote_refuses_when_settings_or_input_changed_after_practice(setup):
    """codex1 검토 [P1] — 연습이 확인한 것과 다른 설정·내용이 실전에 들어가면 안 된다."""
    from pathlib import Path
    make, path = setup
    field = RuneField('target', label='대상')
    controller = make(lambda ctx, inputs: RuneResult(ok=True), fields=[field])
    controller.save_settings('desktop_test', {'target': 'A'})

    practice = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, practice['id'])
    controller.save_settings('desktop_test', {'target': 'B'})
    with pytest.raises(ValueError, match='설정이 바뀌었습니다'):
        controller.promote(practice['id'])

    controller.save_settings('desktop_test', {'target': 'B'})
    practice = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, practice['id'])
    Path(path).write_text('같은 이름, 다른 내용', encoding='utf-8')
    with pytest.raises(ValueError, match='입력 파일 내용'):
        controller.promote(practice['id'])

    practice = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, practice['id'])
    Path(path).unlink()
    with pytest.raises(ValueError, match='입력 파일 내용'):
        controller.promote(practice['id'])


def test_promote_refuses_when_the_installed_rune_changed(setup, tmp_path):
    make, path = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    source = tmp_path / 'src' / 'desktop_changing'
    source.mkdir(parents=True)
    body = ('from vader.sdk import RuneManifest, RuneResult\n'
            'class R:\n'
            '    manifest = RuneManifest("desktop_changing", "바뀌는 룬")\n'
            '    def run(self, ctx, inputs):\n'
            '        return RuneResult(ok=True)\n'
            'rune = R()\n')
    (source / '__init__.py').write_text(body, encoding='utf-8')
    _install(controller, source)
    controller.seal('desktop_changing', '시험')
    practice = controller.submit('desktop_changing', [path], dry_run=True)
    _finish(controller, practice['id'])

    (source / '__init__.py').write_text(body + '# 고침\n', encoding='utf-8')
    _install(controller, source)
    with pytest.raises(ValueError, match='룬이 다시 설치'):
        controller.promote(practice['id'])


def test_two_simultaneous_promotes_make_one_real_run(setup):
    make, path = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    practice = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, practice['id'])
    made, refused, barrier = [], [], threading.Barrier(2)

    def press():
        barrier.wait()
        try:
            made.append(controller.promote(practice['id'])['id'])
        except ValueError:
            refused.append(1)
    threads = [threading.Thread(target=press) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(made) == 1 and len(refused) == 1


def test_failed_real_run_can_be_retried_from_the_same_practice(setup):
    """실전이 실패하면 같은 연습으로 다시 — 성공하면 그 뒤로는 더 넘기지 않는다 (2026-10-12)."""
    make, path = setup
    runs = []

    def rune(ctx, inputs):
        runs.append(ctx.dry_run)
        return RuneResult(ok=len(runs) != 2)   # 연습 성공 → 첫 실전 실패 → 둘째 실전 성공
    controller = make(rune)
    practice = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, practice['id'])

    first = _finish(controller, controller.promote(practice['id'])['id'])
    assert first['state'] == 'failed' and first['retryable'] and first['attempt'] == 1
    assert first['practice'] == practice['id']

    second = _finish(controller, controller.promote(practice['id'])['id'])
    assert second['state'] == 'succeeded' and not second['retryable'] and second['attempt'] == 2
    old = next(j for j in controller.poll() if j['id'] == first['id'])
    assert not old['retryable']                      # 마지막 시도만 다시 할 수 있다
    with pytest.raises(ValueError, match='이미 마쳤습니다'):
        controller.promote(practice['id'])
    assert runs == [True, False, False]


def test_retry_accepts_a_moved_file_only_when_its_content_is_the_same(setup, tmp_path):
    """이온엠은 실패한 파일을 error 폴더로 옮기며 이름을 바꾼다 — 내용이 같으면 받는다."""
    import shutil
    from pathlib import Path
    make, path = setup
    calls = iter([True, False, True])
    controller = make(lambda ctx, inputs: RuneResult(ok=next(calls)))
    practice = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, practice['id'])
    _finish(controller, controller.promote(practice['id'])['id'])          # 실전 실패

    error_dir = tmp_path / 'error'
    error_dir.mkdir()
    moved = error_dir / 'synthetic_20261012_093000.txt'
    shutil.move(path, moved)
    with pytest.raises(ValueError, match='입력 파일 내용'):                 # 원래 자리에는 없다
        controller.promote(practice['id'])
    other = tmp_path / 'other.txt'
    other.write_text('다른 내용', encoding='utf-8')
    with pytest.raises(ValueError, match='연습한 파일과 다릅니다'):
        controller.promote(practice['id'], paths=[str(other)])
    retried = _finish(controller, controller.promote(practice['id'], paths=[str(moved)])['id'])
    assert retried['state'] == 'succeeded' and retried['files'] == [Path(moved).name]


def test_a_result_stays_until_the_person_confirms_it(setup):
    """코어는 확인 안 한 결과를 다시 띄운다 — 확인은 그 표시만 지운다. 실전으로 넘긴 연습은 확인한 것으로 친다."""
    make, path = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    practice = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, practice['id'])
    assert controller.poll()[0]['seen'] is False
    assert controller.confirm_job(practice['id'])['seen'] is True
    running = controller.submit('desktop_test', [path], dry_run=True)
    with pytest.raises(ValueError, match='끝난 작업'):
        controller.confirm_job('없는-작업')
    _finish(controller, running['id'])
    assert {j['id']: j['seen'] for j in controller.poll()}[running['id']] is False


def test_a_practice_sent_to_a_real_run_counts_as_seen(setup):
    make, path = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    practice = controller.submit('desktop_test', [path], dry_run=True)
    _finish(controller, practice['id'])
    real = controller.promote(practice['id'])
    _finish(controller, real['id'])
    seen = {j['id']: j['seen'] for j in controller.poll()}
    assert seen[practice['id']] is True and seen[real['id']] is False


def test_a_console_recording_lands_only_in_the_edited_rune_and_only_what_was_kept(setup, tmp_path, monkeypatch):
    """콘솔 [녹화] (2026-10-27): 시작 때 고정한 편집본 recordings/<이름>.json 에만, 사람이 남긴 단계만 저장한다."""
    import json
    from vader.core import recorder, workshop

    make, _ = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    package = tmp_path / 'workshop' / 'probe' / 'probe'
    monkeypatch.setattr(workshop, 'dev_mode', lambda: True)
    monkeypatch.setattr(workshop, 'package_dir', lambda rune_id: package)
    monkeypatch.setattr(controller.console, 'status', lambda: {'session': 's1', 'alive': True, 'rune_id': 'probe'})

    class Fake:   # 마우스 없이 — 단계 셋을 잡은 녹화
        def __init__(self):
            self.steps = [recorder.Step(at='t', window_title='창', name=f'단추{i}') for i in range(3)]
        def start(self): pass
        def stop(self): return True
    monkeypatch.setattr(recorder, 'Recorder', Fake)

    with pytest.raises(ValueError):
        controller.record_start('다른세션', 'navigation')
    with pytest.raises(ValueError):
        controller.record_start('s1', '../밖')
    controller.record_start('s1', 'navigation')
    with pytest.raises(ValueError):
        controller.record_start('s1', 'navigation')               # 한 번에 하나
    assert len(controller.record_stop()['steps']) == 3
    saved = controller.record_save([0, 2])
    assert saved['count'] == 2 and controller.recording is None
    data = json.loads((package / 'recordings' / 'navigation.json').read_text(encoding='utf-8'))
    assert [s['name'] for s in data['steps']] == ['단추0', '단추2']
    assert not (tmp_path / 'recordings').exists()                 # 옛 전역 위치에는 안 쓴다

    controller.record_start('s1', 'navigation')
    controller.record_stop()
    (package / 'recordings' / 'navigation.json').write_text('{}', encoding='utf-8')   # 녹화 중에 파일이 바뀌었다
    with pytest.raises(ValueError):
        controller.record_save([0])
    controller.record_discard()
    assert controller.recording is None


def test_a_new_rune_console_recording_waits_in_the_workshop(setup, tmp_path, monkeypatch):
    """새 룬을 만드는 콘솔 (2026-10-28): 룬 폴더가 아직 없어 제작 폴더 _recordings 에 둔다 — AI 가 룬을 만들며 옮긴다."""
    from vader.core import recorder, workshop

    make, _ = setup
    controller = make(lambda ctx, inputs: RuneResult(ok=True))
    monkeypatch.setattr(workshop, 'dev_mode', lambda: True)
    monkeypatch.setattr(workshop, 'root', lambda: tmp_path / 'workshop')
    monkeypatch.setattr(controller.console, 'status', lambda: {'session': 's1', 'alive': True, 'rune_id': None})

    class Fake:
        steps = [recorder.Step(at='t', window_title='창', name='단추')]
        def start(self): pass
        def stop(self): return True
    monkeypatch.setattr(recorder, 'Recorder', Fake)

    controller.record_start('s1', 'navigation')
    controller.record_stop()
    saved = controller.record_save([0])
    assert saved['rune_id'] is None
    assert (tmp_path / 'workshop' / '_recordings' / 'navigation.json').is_file()
    assert workshop.projects() == []                               # 제작소 목록에 룬으로 안 잡힌다


def test_tray_watch_notifies_only_while_hidden_and_never_shows_the_message():
    """트레이에 숨은 동안만 코어가 메신저를 받아 알린다. 알림에는 개수만 — 보낸 사람·내용은 넣지 않는다."""
    from types import SimpleNamespace
    from vader.desktop import host
    seen = []
    todo_due = iter([['c1'], [], [], []])                          # 할 일 알림은 보이든 숨든 — 제목 없이 개수만, 띄운 뒤 ack
    api = SimpleNamespace(_hidden=False, call=lambda m, a=None: seen.append(m) or (
        {'ok': True, 'data': next(todo_due)} if m == 'todo_due' else {'ok': True, 'data': {'received': 2}}))
    tray = SimpleNamespace(notes=[], notify=lambda text, title: tray.notes.append((text, title)))
    ticks = iter([None, 'hide', None, 'stop'])

    def sleep(_):
        t = next(ticks)
        if t == 'hide':
            api._hidden = True
        if t == 'stop':
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        host._watch_messenger(api, tray, sleep=sleep)
    assert [m for m in seen if m not in ('todo_due', 'todo_flush')] == ['todo_ack', 'phone_sync', 'messenger_sync', 'messenger_sync']   # 폰은 30초마다
    assert tray.notes == [('다시 알릴 할 일 1개가 있어요. VaderAI 의 할 일에서 확인하세요.', 'VaderAI 할 일')] + [('새 메시지 2개가 왔어요.', 'VaderAI 메신저')] * 2


def test_samples_are_picked_by_name_inside_the_rune_only(setup, tmp_path, monkeypatch):
    """[샘플로 연습] — 화면은 이름만 보내고, 이 룬의 samples/ 밖은 받지 않는다 (codex1 2026-11-19)."""
    make, path = setup
    seen = []
    controller = make(lambda ctx, inputs: seen.extend(i.name for i in inputs) or RuneResult(True))
    folder = tmp_path / 'samples'   # 설치 폴더를 만들면 봉인 검사가 걸린다 — 찾는 자리만 바꾼다
    folder.mkdir()
    (folder / 'a.csv').write_text('x\n1\n', encoding='utf-8')
    monkeypatch.setattr(DesktopController, '_samples', staticmethod(lambda rid: {p.name: p for p in folder.glob('*')}))
    assert next(r for r in controller.catalog()['runes'] if r['id'] == 'desktop_test')['samples'] == ['a.csv']
    for bad in ([path], ['../a.csv'], ['b.csv']):
        with pytest.raises(ValueError):
            controller.submit('desktop_test', bad, samples=True)
    job = controller.submit('desktop_test', ['a.csv'], samples=True)
    assert controller.runner.join(job['id'], 5) and seen == ['a.csv']


def test_a_linked_sample_is_not_offered(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'rune_dir', lambda: tmp_path / 'runes')
    folder = tmp_path / 'runes' / 'r' / 'samples'
    folder.mkdir(parents=True)
    (folder / 'ok.csv').write_text('a\n', encoding='utf-8')
    secret = tmp_path / 'secret.csv'
    secret.write_text('b\n', encoding='utf-8')
    try:
        (folder / 'link.csv').symlink_to(secret)
    except OSError:
        pytest.skip('이 PC 는 링크를 만들 권한이 없다')
    assert list(DesktopController._samples('r')) == ['ok.csv']


def test_a_junctioned_samples_folder_is_not_offered(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(config, 'rune_dir', lambda: tmp_path / 'runes')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.csv').write_text('b\n', encoding='utf-8')
    (tmp_path / 'runes' / 'r').mkdir(parents=True)
    made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(tmp_path / 'runes' / 'r' / 'samples'), str(outside)],
                          capture_output=True)
    if made.returncode:
        pytest.skip('junction 을 만들 수 없는 환경')
    assert DesktopController._samples('r') == {}
