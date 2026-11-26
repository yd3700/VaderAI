"""Thread-safe, JSON-only adapter from the desktop UI to the existing runner."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import RLock

from vader.core import ai_choice, clients, config, installer, rune_text, screen, seal, signing, workshop
from vader.core import catalog as trust_catalog
from vader.core.events import UserActionRequested
from vader.core.installer import install, install_package, preview, uninstall
from vader.core.loader import discover
from vader.core.view import JobView, apply, coerce_field, seal_state
from vader.desktop.console import Console
from vader.sdk import CheckpointKey, Input, RuneResult, UserActionResponse


def _check_rows(value) -> None:
    """표 편집 답의 **모양**만 본다. 업무 검사(합계·코드)는 룬이 한다."""
    import json

    if len(str(value)) > 200_000:
        raise ValueError('표가 너무 큽니다.')
    try:
        rows = json.loads(value)
    except ValueError:
        raise ValueError('표를 읽지 못했습니다.') from None
    if not isinstance(rows, list) or len(rows) > 500 or not all(isinstance(r, dict) for r in rows):
        raise ValueError('표를 읽지 못했습니다.')


class DesktopController:
    def __init__(self, runner, *, loaded=None):
        self.runner = runner
        runes, failures = discover() if loaded is None else (loaded, [])
        self.runes = {r.manifest.id: r for r in runes}
        _hide_secrets(self.runes)
        _fill_first_settings(self.runes)
        self.failures = [asdict(f) for f in [*installer.bundle_failures, *failures]]
        self.lock = RLock()
        self.jobs = {}
        self.active = None
        self.recording = None   # 콘솔의 [녹화] 한 건 — 시작 때 세션·룬·저장 위치를 고정한다
        self.screen_acting = False   # 화면 자동화가 승인된 조작을 보내는 중 — 작업·녹화를 시작하지 않는다
        self.closing = False
        self.console = Console()

    def catalog(self):
        with self.lock:
            items = []
            hidden = set(config.get('desktop.hidden_runes') or [])
            for key, rune in self.runes.items():
                params = config.rune_config(key)
                state = seal_state(key)
                fields = []
                for f in rune.manifest.required_fields:
                    data = asdict(f)
                    data['value'] = '' if f.kind == 'secret' else params.get(f.name, f.default)
                    data['configured'] = f.name in params and params[f.name] not in ('', None)
                    fields.append(data)
                items.append({**asdict(rune.manifest), 'fields': fields,
                              'locked': state.locked, 'reason': state.reason, 'enabled': key not in hidden,
                              'mapping': callable(getattr(rune.rune, 'mapping_maintenance', None)),
                              'recordings': sorted(p.stem for p in (config.rune_dir() / key / 'recordings').glob('*.json')),
                              # 룬에 실린 연습용 샘플 — 파일 없이 [샘플로 연습] (기본 룬 2026-11-19)
                              'samples': list(self._samples(key)),
                              # 화면 문구의 영어 — 그 버전 룬 패키지 안 i18n_en.py, 실행하지 않고 값만 (영문판 5단계 후속)
                              'i18n_en': rune_text.load(config.rune_dir() / key)})
            dev = config.get('workshop.dev_mode') is True
            return {'runes': items, 'failures': self.failures, 'dev_mode': dev,
                    'clients': clients.status() if dev else [], 'console': self.console.status(),
                    'test_requests': workshop.test_requests(),
                    'ai_ready': ai_choice.ready(), 'projects': workshop.projects(),
                    **self.catalog_alerts()}

    # ── 앱 로그인 (vader/core/account.py) ──

    def account_status(self):
        from vader.core import account
        if not account.signed_in():
            return {'signed_in': False}
        try:
            found = account.account()
        except account.AccountError as exc:     # 네트워크 문제 — 로그인은 그대로, 확인만 못 했다
            return {'signed_in': True, 'error': str(exc)}
        # 끊겼거나 계정이 막혔으면 코어가 토큰을 이미 버렸다 — 화면도 로그아웃 상태로 (codex1 P2-2)
        return {'signed_in': True, 'account': found} if found else {'signed_in': False}

    # ── 광장 받기 (vader/core/catalog_sync.py) ──

    def market_list(self, refresh=False):
        from vader.core import catalog_sync
        if refresh:
            catalog_sync.refresh()
        from vader.core import account
        me = account.account() or {}
        return {**catalog_sync.listing(), 'publisher': me.get('id') in trust_catalog.PUBLISHERS}   # [공유] 는 발행자에게만

    # 광장 공유 범위 (2026-11-19, 구글 시트처럼) — 서버가 발행자인지 다시 본다
    def market_shares(self):
        from vader.core import account, catalog_sync
        try:
            return catalog_sync.shares()
        except account.AccountError as exc:
            raise ValueError(str(exc)) from None

    def market_share_save(self, rune_id, restricted, accounts):
        from vader.core import account, catalog_sync
        if type(restricted) is not bool or not isinstance(accounts, list):
            raise ValueError('공유 설정을 다시 골라 주세요.')
        try:
            return catalog_sync.save_share(str(rune_id), restricted, [str(a) for a in accounts])
        except account.AccountError as exc:
            raise ValueError(str(exc)) from None

    def market_install(self, rune_id, version, archive_sha256=None, replace_local=False):
        """목록에서 받아 설치. 설치는 실전 승인이 아니다 — 연습한 뒤 사람이 승인한다.
        archive_sha256 — 확인 창에서 본 패키지. 다르면 받지 않는다 (codex1 2026-11-25)."""
        from vader.core import catalog, catalog_sync
        with self.lock:
            self._idle('룬을 설치하세요')
            replace_local = replace_local is True     # 명시적인 참만 동의 (codex1 회신 P2-3)
            if replace_local:      # 직접 설치한 룬을 더 새 광장 버전으로만 — 화면 값을 믿지 않고 다시 본다
                rune = self.runes.get(str(rune_id))
                if (rune is None or not catalog.local_rune(str(rune_id))
                        or catalog._version(str(version)) <= catalog._version(rune.manifest.version)):
                    raise ValueError('바꿀 수 있는 직접 설치한 룬이 아니에요. 화면을 새로 고쳐 주세요.')
            try:
                done = catalog_sync.install(str(rune_id), str(version),
                                           archive_sha256=None if archive_sha256 is None else str(archive_sha256),
                                           replace_local=replace_local)
            except catalog_sync.SyncError as exc:
                raise ValueError(str(exc)) from None
            self._reload()
            return {**self.catalog(), 'market': catalog_sync.listing(), 'installed': {
                'id': done.manifest.id, 'name': done.manifest.name, 'version': done.manifest.version, 'change': done.change,
                'granted': done.granted, 'missing': done.missing_settings,
                'kept': (catalog.origins().get(done.manifest.id) or {}).get('replaced_local') if replace_local else None}}

    def catalog_alerts(self):
        """목록 안내와 재시작 경고 — 화면이 몇 초마다 따로 묻는다 (작업 중에도 회수가 도착하게, codex1 P2-1).

        이미 불러온 룬이 악성 등급이 되면 실행 중이 아니어도 재시작을 경고한다 — 코드는 이미 import 됐다.
        """
        from vader.core import catalog_sync
        catalog_sync.refresh_if_due()            # 켜 있는 동안 하루 여러 번 — 회수·만료가 늦지 않게 (계약 3절)
        exit_required = self.runner.exit_required
        if exit_required is None:
            for key in self.runes:
                allowed = trust_catalog.verdict(key)
                if allowed.get('level') == 'malicious':
                    exit_required = {'rune_id': key, 'message': allowed['reason'] + ' 이미 불러온 상태입니다. '
                                     '앱을 닫았다가 다시 여세요.'}
                    break
        from vader.core import publisher
        renew = publisher.renewal_notice()      # 서명 PC 에만 원장이 있다
        return {'catalog_notices': trust_catalog.notices() + ([renew] if renew else []), 'exit_required': exit_required,
                'updates': self._market_updates()}

    def _market_updates(self):
        """내 룬에 [업데이트] 를 띄울 룬 — 광장에 더 새 판이 있는 것만. 지금 받아 둔 목록만 보고 통신하지 않는다
        (자동 온라인 확인을 끈 사람에게 화면을 여는 것만으로 묻지 않는다, codex1 (e)). 늘어나는 권한도 함께."""
        from vader.core import catalog, catalog_sync, permissions
        out = {}
        access = catalog_sync.confirmed_access()
        if access is None:
            return out          # 이 계정으로 받을 수 있는지 모른다 — 광장 [목록 새로 고침] 으로 확인 (codex1 2026-11-25)
        for r in catalog_sync.listing().get('releases', []):
            rune = self.runes.get(r['rune_id'])
            if rune is None or r['rune_id'] not in access:
                continue
            # 직접 설치한 같은 룬도 광장 판이 더 새로우면 — 확인 창에서 '바꾸기' 로 묻는다 (마스터 2026-11-26)
            local = r['state'] == 'local' and catalog._version(r['version']) > catalog._version(rune.manifest.version)
            if r['state'] != 'update' and not local:
                continue
            have = set(rune.manifest.permissions)
            added = [p for p in r['permissions'] if p not in have]
            out[r['rune_id']] = {'version': r['version'], 'installed': rune.manifest.version if local else r['installed'],
                                 'archive_sha256': r['archive_sha256'], 'name': r['name'],
                                 'added': permissions.describe(added) if added else [], **({'local': True} if local else {})}
        return out

    def auto_online(self):
        return {'on': config.auto_online()}

    def set_auto_online(self, on):
        """자동 업데이트 확인·광장 목록 자동 받기. 끄면 광장의 [새로 고침]·받기는 누를 때만 통신한다."""
        config.device_set('network.auto_online', None if on else False)
        return self.auto_online()

    def set_dev_mode(self, on):
        """룬 제작소 개발 모드 — 켜져 있을 때만 MCP 시험이 만든 룬 코드를 돌린다. 보안 경계는 아니다."""
        config.set_dev_mode(bool(on))
        return self.catalog()

    def set_rune_enabled(self, rune_id, on):
        """내 룬의 체크 — 끈 룬은 코어 화면에서 빠진다. 설치·승인은 그대로다."""
        if rune_id not in self.runes:
            raise ValueError('설치되지 않은 룬입니다.')
        config.set_rune_enabled(str(rune_id), bool(on))
        return self.catalog()

    def install_client(self, client):
        """룬을 만들 프로그램을 winget 으로 깐다. 사람이 확인 창에서 누른 뒤에만 불린다."""
        clients.install(client)
        return self.catalog()

    def connect_client(self, client):
        """룬을 만들 프로그램에 VaderAI 를 등록한다. 사람이 확인 창에서 누른 뒤에만 불린다."""
        clients.connect(client)
        return self.catalog()

    # 제작소 안 콘솔 — 세션은 Console 이 갖고, 여기는 통로만 (vader/desktop/console.py).
    def console_start(self, client, cols, rows, rune_id=None):
        return self.console.start(client, cols, rows, str(rune_id) if rune_id else None)

    def edit_rune(self, rune_id):
        """내 룬의 [수정] — 제작소로 가져온다. 이미 고치는 중이면 그대로 연다."""
        workshop.edit(str(rune_id))
        return self.catalog()

    def link_source(self, rune_id, folder):
        workshop.link_source(str(rune_id), Path(folder))
        return self.catalog()

    def import_recording(self, rune_id, name):
        workshop.import_recording(str(rune_id), str(name))
        return self.catalog()

    def console_read(self, session):
        return self.console.read(session)

    def console_write(self, session, data):
        return self.console.write(session, data)

    def console_resize(self, session, cols, rows):
        return self.console.resize(session, cols, rows)

    def console_stop(self, session):
        return self.console.stop(session)

    def console_paste_image(self, session, data):
        return self.console.save_image(session, data)

    def clipboard(self):
        """사람이 콘솔에서 붙여넣기를 눌렀을 때만 — 글자와 탐색기에서 복사한 파일 경로."""
        from vader.desktop import clipboard
        return clipboard.read()

    # 콘솔의 [녹화] (2026-10-27 시험 PC 피드백, codex1 검토). 시작 때 저장 위치를 고정하고,
    # 확실히 멈춘 녹화를 사람이 거른 뒤 **편집 중인 룬 안** recordings/<이름>.json 에만 저장한다. 설치본에는 안 쓴다.
    def record_start(self, session, name):
        from vader.core import recorder
        status = self.console.status()
        if not workshop.dev_mode() or not status['alive'] or status['session'] != session:
            raise ValueError('열린 콘솔에서만 녹화할 수 있어요.')
        if not recorder._NAME.fullmatch(str(name or '')):
            raise ValueError('녹화 이름은 영문 소문자로 시작하는 소문자·숫자·밑줄입니다 (예: navigation).')
        with self.lock:
            if self.recording:
                raise ValueError('이미 녹화 중이에요.')
            if self.busy() or self.screen_acting:
                raise ValueError('작업이 실행 중이라 녹화할 수 없어요.')
            screen.stop_plan('녹화가 시작됐어요.')
            # 고치는 룬이면 그 룬 안에. 새 룬을 만드는 콘솔이면 아직 폴더가 없다 — 제작 폴더의 임시 자리에 두고
            # AI 가 룬을 만들 때 옮긴다 (마스터 결정 2026-10-28). `_` 로 시작해 제작소 목록에 룬으로 안 잡힌다.
            home = workshop.package_dir(status['rune_id']) / 'recordings' if status['rune_id'] else workshop.root() / '_recordings'
            target = home / f'{name}.json'
            made = recorder.Recorder()
            made.start()
            self.recording = {'session': session, 'rune_id': status['rune_id'], 'name': name, 'target': target,
                              'before': _digest(target), 'recorder': made, 'steps': None}
            return {'name': name, 'path': str(target), 'exists': target.exists()}

    def record_stop(self):
        with self.lock:
            r = self.recording
            if not r:
                raise ValueError('녹화 중이 아니에요.')
            if r['steps'] is None:
                if not r['recorder'].stop():
                    raise ValueError('녹화가 아직 멈추지 않았어요. 잠시 뒤 다시 끝내기를 눌러 주세요.')
                r['steps'] = list(r['recorder'].steps)
            return {'name': r['name'], 'path': str(r['target']), 'exists': r['target'].exists(),
                    'steps': [{'label': s.label(), 'findable': s.findable()} for s in r['steps']]}

    def record_save(self, keep):
        from vader.core import recorder
        with self.lock:
            r = self.recording
            if not r or r['steps'] is None:
                raise ValueError('끝낸 녹화가 없어요.')
            if not isinstance(keep, list) or any(type(i) is not int or not 0 <= i < len(r['steps']) for i in keep):
                raise ValueError('남길 단계를 다시 골라 주세요.')
            steps = [r['steps'][i] for i in sorted(set(keep))]
            if not steps:
                raise ValueError('남길 단계가 없어요.')
            if _digest(r['target']) != r['before']:
                raise ValueError('녹화하는 동안 같은 이름의 녹화 파일이 바뀌었어요. 버리고 다시 녹화해 주세요.')
            r['target'].parent.mkdir(parents=True, exist_ok=True)
            recorder.save(r['name'], steps, r['target'])
            self.recording = None
            return {'name': r['name'], 'path': str(r['target']), 'count': len(steps), 'session': r['session'],
                    'rune_id': r['rune_id']}

    def record_discard(self):
        with self.lock:
            r, self.recording = self.recording, None
        if r:
            r['recorder'].stop()
        return True

    def allow_test(self, rune_id, fingerprint):
        """사람이 코드 요약을 보고 그 버전만 이 PC 에서 시험하게 한다 (마스터 결정 2026-10-14)."""
        workshop.allow_test(str(rune_id), str(fingerprint))
        return self.catalog()

    def _reload(self):
        runes, failures = discover()
        self.runes = {r.manifest.id: r for r in runes}
        _hide_secrets(self.runes)
        _fill_first_settings(self.runes)
        self.failures = [asdict(f) for f in [*installer.bundle_failures, *failures]]

    def _idle(self, what):
        if self.busy():
            raise ValueError(f'실행이 끝난 뒤 {what}.')

    def seal(self, rune_id, note=''):
        """사람이 '연습 실행으로 확인했다' 고 승인하는 자리. 누가·언제·무엇을 원장에 남긴다."""
        with self.lock:
            self._idle('승인하세요')
            source = config.rune_dir() / rune_id
            if rune_id not in self.runes or not source.is_dir():
                raise ValueError('설치 폴더를 찾지 못해 승인할 수 없습니다.')
            if not seal.loadable(rune_id, source):   # 설치 때 확인한 코드가 아니다
                raise ValueError('설치한 뒤 룬 파일이 바뀌었습니다. 룬 설치로 다시 설치한 뒤 승인하세요.')
            seal.seal(rune_id, source, by=_who(), note=str(note))
            return self.catalog()

    def uninstall(self, rune_id):
        """룬만 뗀다. 설정·체크포인트·학습 데이터는 남는다 — 다시 설치하면 그대로 쓴다."""
        with self.lock:
            self._idle('제거하세요')
            if not uninstall(rune_id):
                raise ValueError('설치돼 있지 않은 룬입니다.')
            self._reload()
            return self.catalog()

    def ai_choice(self):
        """AI 선택 화면. 키 값은 오지 않는다 — 있는지만."""
        return ai_choice.view()

    def ai_connect(self, provider, key=None, model=None):
        """키(또는 저장된 키)로 연결 확인 → 통과하면 저장. 실행 중에는 바꾸지 않는다 (codex1 지적).
        확인(네트워크)은 잠금 밖에서, 저장은 잠금 안에서 다시 확인한 뒤 — 확인하는 사이 작업이 시작될 수 있다."""
        with self.lock:
            self._idle('AI 연결을 바꾸세요')
        return ai_choice.connect(str(provider), key or None, model or None, guard=self._ai_saving)

    def ai_add(self, provider, model):
        """연결된 서비스에 모델 하나 더하기."""
        with self.lock:
            self._idle('AI 를 더하세요')
        return ai_choice.add_model(str(provider), str(model or ''), guard=self._ai_saving)

    @contextmanager
    def _ai_saving(self):
        with self.lock:
            self._idle('AI 연결을 바꾸세요')
            yield

    def ai_default(self, provider):
        with self.lock:
            self._idle('기본 AI 를 바꾸세요')
            return ai_choice.set_default(str(provider))

    def ai_disconnect(self, provider):
        with self.lock:
            self._idle('AI 연결을 끊으세요')
            return ai_choice.disconnect(str(provider))

    def _mapping(self, rune_id):
        rune = self.runes[rune_id].rune
        factory = getattr(rune, 'mapping_maintenance', None)
        if not callable(factory):
            raise ValueError('이 룬에는 매핑 관리가 없습니다.')
        master_db = Path(str(config.rune_config(rune_id).get('master_db', '')))
        return factory(config.data_dir() / 'rune-data' / rune_id, config.data_dir() / 'jobs', master_db)

    def mapping(self, rune_id):
        """최근 작업의 확인필요·AI추정 항목과 학습 DB. 무엇을 어떻게 저장할지는 룬이 정한다."""
        with self.lock:
            service = self._mapping(rune_id)
            return {'review': [{'review': i.review, 'insurance_code': i.insurance_code,
                                'name': i.learning_key, 'current_code': i.current_code,
                                'scope': i.scope} for i in service.review_items()],
                    'learned': [{'vendor': r['vendor'], 'name': r['item_name'], 'code': r['mapped_code']}
                                for r in service.learned_rows()],
                    'dept': service.dept_memory() if hasattr(service, 'dept_memory') else None}

    def mapping_dept(self, rune_id, key, kind):
        """입고 종류 기억 한 줄을 바꾸거나(kind) 그 줄만 지운다(빈 kind).

        작업 중에는 막는다 — 진행 중인 명세서의 옛 답이 저장 뒤 같은 줄을 다시 덮는다 (codex1).
        """
        with self.lock:
            if self.active and not self.jobs[self.active]['job'].is_terminal:
                raise ValueError('작업이 끝난 뒤에 바꿔 주세요. 진행 중인 입고가 같은 기억을 쓰고 있습니다.')
            service = self._mapping(rune_id)
            if not hasattr(service, 'set_dept'):
                raise ValueError('이 룬에는 입고 종류 기억이 없습니다.')
            service.set_dept(str(key), str(kind or ''))
            return self.mapping(rune_id)

    def mapping_correct(self, rune_id, index, code):
        with self.lock:
            service = self._mapping(rune_id)
            items = service.review_items()
            if type(index) is not int or not 0 <= index < len(items):
                raise ValueError('목록이 바뀌었습니다. 다시 불러온 뒤 고르세요.')
            scope = service.correct(items[index], str(code))
            return {'scope': scope, **self.mapping(rune_id)}

    def mapping_save(self, rune_id, edits):
        """학습 행 여러 개의 거래처·품목명·코드를 한 번에. 하나라도 틀리면 아무것도 안 바뀐다."""
        with self.lock:
            service = self._mapping(rune_id)
            if not hasattr(service, 'edit_learning'):
                raise ValueError('이 룬은 학습 행 일괄 수정을 지원하지 않습니다.')
            if not isinstance(edits, list) or not edits:
                raise ValueError('바뀐 내용이 없습니다.')
            count = service.edit_learning([
                {'vendor': e['vendor'], 'item_name': e['name'], 'new_vendor': e['new_vendor'],
                 'new_item_name': e['new_name'], 'new_code': e['new_code']} for e in edits])
            return {'count': count, **self.mapping(rune_id)}

    def mapping_delete(self, rune_id, items):
        """고른 학습 행만 지운다. 코드가 같아도 고르지 않은 행은 남는다 (룬이 품목명으로 고른다)."""
        with self.lock:
            service = self._mapping(rune_id)
            if not isinstance(items, list) or not items:
                raise ValueError('지울 품목을 선택해 주세요.')
            count = sum(bool(service.delete_learning(i['vendor'], i['name'])) for i in items)
            return {'count': count, **self.mapping(rune_id)}

    def preview(self, source):
        """설치 전에 보여 줄 요약. 코드를 실행하지 않는다. 사람이 이것을 보고 설치를 누른다."""
        source = _rune_source(source)
        return {'path': str(source), **preview(source)}

    def create_key(self, name, passphrase):
        """운영자 서명 키. 암호는 저장하지 않는다 — 키 파일을 잠그는 데만 쓴다."""
        signing.create_key(str(name), str(passphrase))
        return self.catalog()

    def sign(self, source, expect, passphrase):
        """사람이 요약과 시험 기록을 본 바로 그 패키지(`expect`)에만 서명한다."""
        source = Path(source)
        if source.suffix.lower() != '.zip' or preview(source)['fingerprint'] != expect:
            raise ValueError('확인한 뒤에 패키지가 바뀌었습니다. 다시 골라 주세요.')
        return signing.sign(source, str(passphrase))

    def preview_project(self, rune_id):
        """제작소에서 포장한 그 zip 의 설치 요약. 고르기 창 없이 — 경로는 제작 공간 규칙으로만 만든다."""
        found = next((p for p in workshop.projects() if p['id'] == rune_id), None)
        if found and found['profile'] == 'legacy':
            # 화면 자동화 같은 기존 방식 룬은 파일 사례 시험·포장이 없다 — 고친 폴더를 이 PC 에 그대로 설치한다 (codex1 합의)
            return self.preview(workshop.package_dir(rune_id))
        if not found or not found['packed']:
            raise ValueError('포장된 패키지가 없습니다. 시험을 통과한 뒤 포장하세요.')
        return self.preview(workshop.project(rune_id) / 'dist' / f"{rune_id}-{found['version']}.zip")

    def shared_path(self, file):
        """광장의 '위치 열기'. 목록에 있는 공유본만 — 화면이 아무 경로나 열게 하지 않는다."""
        for item in signing.shared(workshop.root()):
            if item['file'] == file:
                return Path(item['path'])
        raise ValueError('공유본을 찾지 못했습니다. 목록을 새로 고쳐 주세요.')

    def install(self, source, expect, trust_signer=False):
        """사람이 요약을 보고 확인한 지문(`expect`)의 룬만 설치하고 목록을 다시 읽는다. 검사·복사·되돌리기는 설치기가 한다."""
        with self.lock:
            self._idle('룬을 설치하세요')
            source = _rune_source(source)
            done = (install_package(source, expect=expect, trust_signer=bool(trust_signer)) if source.suffix.lower() == '.zip'
                    else install(source, expect=expect))
            self._reload()
            return {**self.catalog(), 'installed': {
                'id': done.manifest.id, 'name': done.manifest.name, 'version': done.manifest.version, 'change': done.change,
                'granted': done.granted, 'missing': done.missing_settings}}

    def save_settings(self, rune_id, values):
        with self.lock:
            if self.active and not self.jobs[self.active]['job'].is_terminal:
                raise ValueError('실행이 끝난 뒤 설정을 변경하세요.')
            rune = self.runes[rune_id]
            current = config.rune_config(rune_id)
            clean = {}
            for f in rune.manifest.required_fields:
                raw = values.get(f.name, '')
                if f.kind == 'secret' and not raw and current.get(f.name):
                    continue
                clean[f.name] = coerce_field(f, str(raw))
            config.set_rune_configs(rune_id, clean, secret=_secret_fields(rune))
            return self.catalog()

    @staticmethod
    def _samples(rune_id):
        """룬에 실린 연습용 샘플 이름 → 경로. 화면에는 이름만 준다 (codex1 2026-11-19)."""
        folder = config.rune_dir() / rune_id / 'samples'
        real = config.rune_dir().resolve() / rune_id / 'samples'
        # junction(reparse) 은 is_symlink 가 놓친다 — 실제 자리가 설치 폴더의 samples 그대로인지 본다 (codex1 회신 3)
        if not folder.is_dir() or folder.resolve() != real:
            return {}
        # 링크는 받지 않는다 — 샘플 이름으로 폴더 밖 파일을 읽게 하지 않는다 (codex1 회신 2)
        return {p.name: p for p in sorted(folder.iterdir())
                if p.is_file() and not p.is_symlink() and p.resolve().parent == real}

    def submit(self, rune_id, paths, dry_run=True, start_page=1, samples=False):
        with self.lock:
            if self.closing:
                raise ValueError('앱을 종료하는 중입니다.')
            if self.active and not self.jobs[self.active]['job'].is_terminal:
                raise ValueError('진행 중인 작업이 있습니다. 완료하거나 중단한 뒤 시작하세요.')
            if self.screen_acting:
                raise ValueError('화면 조작을 보내는 중이에요. 잠시 뒤 시작하세요.')
            screen.stop_plan('룬 작업이 시작됐어요.')   # 화면 묶음 계획 중간 개입 → 계획 끝
            if type(dry_run) is not bool:
                raise ValueError('실행 모드를 선택하세요.')
            rune = self.runes[rune_id]
            if not isinstance(paths, list) or not paths:
                raise ValueError('처리할 파일을 선택하세요.')
            if samples is True:   # 이름만 받아 이 룬의 samples/ 에서 찾는다 — 다른 경로는 받지 않는다
                found = self._samples(rune_id)
                if not all(isinstance(p, str) and p in found for p in paths):
                    raise ValueError('이 룬에 실린 샘플이 아닙니다.')
                paths = [str(found[p]) for p in paths]
            inputs = []
            for raw in dict.fromkeys(paths):
                path = Path(raw).resolve(strict=True)
                if not path.is_file():
                    raise ValueError('폴더 대신 파일을 선택하세요.')
                inputs.append(Input(path=path))
            params = config.rune_config(rune_id)
            for f in rune.manifest.required_fields:
                if f.name not in params and not f.optional:
                    raise ValueError(f'{f.label or f.name} 설정을 먼저 저장하세요.')
                if f.name in params:
                    coerce_field(f, str(params[f.name]))
            page = int(start_page)
            if page < 1:
                raise ValueError('시작 페이지는 1 이상이어야 합니다.')
            practiced = self._identity(rune_id, inputs) if dry_run else None
            params = {**params, 'start_page': page}
            job = self.runner.submit(rune.rune, inputs, params=params, dry_run=dry_run)
            self.jobs[job.id] = {'job': job, 'view': JobView(), 'request': None,
                                 'dry_run': dry_run, 'start_page': page, 'practiced': practiced,
                                 'real': None, 'attempts': 0, 'practice': None, 'seen': False}
            self.active = job.id
            return self._snapshot(job.id)

    @staticmethod
    def _identity(rune_id, inputs):
        """연습이 무엇으로 돌았나 — 설정 · 입력 파일 **내용** · 설치된 룬 코드.

        같은 경로는 같은 문서가 아니다. 이름이 같아도 내용이 바뀌었으면 연습이 확인한 것이 아니다.
        """
        settings = json.dumps(config.rune_config(rune_id), sort_keys=True, default=str, ensure_ascii=False)
        try:
            contents = CheckpointKey.from_inputs(rune_id, inputs).input_identity
        except OSError:
            contents = None   # 사라졌거나 못 읽는다 — 아래 비교에서 '바뀜' 으로 걸린다
        return {'rune': seal.fingerprint(config.rune_dir() / rune_id),
                'settings': hashlib.sha256(settings.encode('utf-8')).hexdigest(),
                'inputs': contents}

    def promote(self, job_id, paths=None):
        """끝난 연습 실행을 **같은 파일·같은 시작 페이지로** 실전 실행한다.

        연습 뒤 룬 · 설정 · 입력 파일 내용 중 하나라도 바뀌었으면 거부하고 다시 연습하게 한다 —
        연습이 확인한 것과 다른 것이 실전에 들어가지 않게. 승인 전 룬은 러너가 거부한다.

        **실전이 실패·중단으로 끝났으면 같은 연습으로 다시 넘길 수 있다.** 이미 입력한 건은
        룬의 진행 기록(입력 내용이 열쇠)이 건너뛴다. 이온엠이 실패한 파일을 error 폴더로 옮기며
        이름을 바꾸므로 `paths` 로 다시 고를 수 있다 — **내용이 연습한 파일과 같을 때만** 받는다.
        연습 기록은 이 앱이 켜져 있는 동안만 남는다 (마스터 결정 2026-10-12: 다시 켜면 연습부터).
        """
        with self.lock:
            record = self.jobs.get(job_id)
            if record is None:
                raise ValueError('작업을 찾을 수 없습니다.')
            job = record['job']
            if not record['dry_run'] or job.state.value != 'succeeded':
                raise ValueError('성공한 연습 실행만 실전으로 이어서 할 수 있습니다.')
            last = self.jobs[record['real']]['job'] if record['real'] else None
            if last is not None and not last.is_terminal:
                raise ValueError('실전 실행이 아직 진행 중입니다.')
            if last is not None and last.state.value == 'succeeded':
                raise ValueError('이 연습으로 실전 실행을 이미 마쳤습니다.')
            inputs = job.inputs if paths is None else [Input(path=Path(p)) for p in dict.fromkeys(paths)]
            if not inputs:
                raise ValueError('처리할 파일을 선택하세요.')
            now = self._identity(job.rune_id, inputs)
            for key, what in (('rune', '룬이 다시 설치되어 바뀌었습니다'),
                              ('settings', '룬 설정이 바뀌었습니다'),
                              ('inputs', '입력 파일 내용이 바뀌었거나 없어졌습니다')):
                if now[key] != record['practiced'][key] or (key == 'inputs' and now[key] is None):
                    if key == 'inputs' and paths is not None:
                        raise ValueError('고른 파일의 내용이 연습한 파일과 다릅니다. 연습한 그 파일을 골라 주세요.')
                    raise ValueError(f'연습 뒤 {what}. 지금 상태로 다시 연습 실행한 뒤 실전으로 넘겨 주세요.')
            try:
                snapshot = self.submit(job.rune_id, [str(i.path) for i in inputs],
                                       dry_run=False, start_page=record['start_page'])
            except RuntimeError as exc:   # 승인 전 — 러너의 자물쇠
                raise ValueError(str(exc)) from exc
            record['real'] = snapshot['id']
            record['seen'] = True   # 실전으로 넘겼으면 그 연습 결과는 본 것이다 — 코어에 다시 띄우지 않는다
            record['attempts'] += 1
            real = self.jobs[snapshot['id']]
            real['practice'], real['attempts'] = job_id, record['attempts']
            return self._snapshot(snapshot['id'])

    def poll(self):
        with self.lock:
            for event in self.runner.drain_events():
                record = self.jobs.get(event.job_id)
                if record is None:
                    continue
                apply(record['view'], event)
                if isinstance(event, UserActionRequested):
                    record['request'] = {'id': event.correlation_id, **asdict(event.request)}
            return [self._snapshot(key) for key in reversed(self.jobs)]

    def _snapshot(self, key):
        record = self.jobs[key]
        job, view = record['job'], record['view']
        result = job.result if isinstance(job.result, RuneResult) else None
        if job.is_terminal:
            record['request'] = None
        return {'id': key, 'rune_id': job.rune_id, 'state': job.state.value,
                'terminal': job.is_terminal, 'dry_run': record['dry_run'],
                'files': [i.path.name for i in job.inputs], 'promoted': bool(record.get('real')),
                'practice': record.get('practice'), 'attempt': record.get('attempts', 0),
                'retryable': self._retryable(record), 'seen': record['seen'],
                'current': view.current, 'total': view.total, 'message': view.message,
                'logs': view.logs, 'request': record['request'],
                'failure': asdict(job.failure) if job.failure else None,
                'summary': result.summary if result else {},
                'failures': [asdict(f) for f in result.failures] if result else [],
                'artifacts': [Path(p).name for p in result.artifacts] if result else []}

    def _retryable(self, record):
        """이 실전이 연습에서 왔고, 그 연습의 **마지막** 시도이고, 실패·중단으로 끝났나."""
        practice = self.jobs.get(record.get('practice'))
        job = record['job']
        return bool(practice and practice['real'] == job.id and job.is_terminal
                    and job.state.value in ('failed', 'cancelled'))

    def confirm_job(self, job_id):
        """결과를 봤다. 코어가 이 결과를 다시 띄우지 않게 할 뿐 — 승인·삭제·재실행과 무관하다."""
        with self.lock:
            record = self.jobs.get(job_id)
            if record is None or not record['job'].is_terminal:
                raise ValueError('끝난 작업만 확인할 수 있습니다.')
            record['seen'] = True
            return self._snapshot(job_id)

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError('작업을 찾을 수 없습니다.')
            return self.runner.cancel(job_id)

    def respond(self, job_id, request_id, action, value=None):
        with self.lock:
            request = self.jobs[job_id]['request']
            if not request or request['id'] != request_id:
                raise ValueError('이미 처리되었거나 만료된 확인 요청입니다.')
            allowed = {'skip', 'abort'}
            allowed.add('corrected' if request['kind'] in ('correct_value', 'edit_rows') else 'ok')
            if action not in allowed:
                raise ValueError('지원하지 않는 응답입니다.')
            if action == 'corrected' and not str(value or '').strip():
                raise ValueError('수정할 값을 입력하세요.')
            if action == 'corrected' and request['kind'] == 'edit_rows':
                _check_rows(value)
            if action == 'ok' and request['kind'] == 'choice' and value not in request['options']:
                raise ValueError('목록에서 선택하세요.')
            delivered = self.runner.respond(job_id, request_id, UserActionResponse(action, value))
            if not delivered:
                raise ValueError('확인 요청이 만료되었습니다. 상태를 다시 확인하세요.')
            self.jobs[job_id]['request'] = None
            return True

    def artifact(self, job_id, index):
        with self.lock:
            job = self.jobs[job_id]['job']
            if not isinstance(job.result, RuneResult) or type(index) is not int or index < 0:
                raise ValueError('결과 파일을 찾을 수 없습니다.')
            return Path(job.result.artifacts[index]).resolve(strict=True)

    def busy(self):
        with self.lock:
            return bool(self.active and not self.jobs[self.active]['job'].is_terminal)

    @contextmanager
    def screen_hold(self):
        """화면 자동화 조작 하나의 실행권 — 룬 작업·녹화와 한 번에 하나 (codex1 화면 2단계 계약 5)."""
        with self.lock:
            if self.closing or self.busy() or self.recording or self.screen_acting:
                raise ValueError('다른 작업이나 녹화가 진행 중이라 보내지 않았어요.')
            self.screen_acting = True
        try:
            yield
        finally:
            with self.lock:
                self.screen_acting = False

    def stop_screen_work(self):
        """Windows 가 잠기거나 잠들었다 — 화면을 조작하는 작업은 다음 안전 지점에서 멈춘다 (codex1).
        자동으로 이어 돌리지 않는다. 다시 실행은 사람이 결과 화면에서 — 이미 넣은 건은 룬이 건너뛴다."""
        with self.lock:
            record = self.jobs.get(self.active) if self.active else None
            job = record and record['job']
            rune = job and self.runes.get(job.rune_id)
            if job and not job.is_terminal and rune and rune.manifest.target_apps:
                self.runner.cancel(job.id)

    def begin_close(self):
        with self.lock:
            if self.busy():
                return False
            self.closing = True
        self.record_discard()   # 창을 닫으면 녹화도 끝 — 화면 없이 수집이 계속되지 않게
        return True


def _digest(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _secret_fields(rune):
    return [f.name for f in rune.manifest.required_fields if f.kind == 'secret']


def _hide_secrets(runes):
    """settings.json 에 평문으로 남은 룬 비밀번호를 Windows 보호 저장으로 옮긴다 (2026-10-21)."""
    for key, rune in runes.items():
        try:
            config.migrate_rune_secrets(key, _secret_fields(rune))
        except Exception:
            pass   # 못 옮기면 평문 그대로 돈다 — 앱이 안 켜지는 것보다 낫다


def _fill_first_settings(runes):
    """룬이 `first_settings(datadir)` 를 주면 **비어 있는 설정 키만** 채운다 — 새 PC 에서 손대지 않고 바로 연습 실행까지 가게."""
    for key, rune in runes.items():
        first = getattr(rune.rune, 'first_settings', None)
        if not callable(first):
            continue
        try:
            current = config.rune_config(key)
            missing = {k: v for k, v in first(config.data_dir() / 'rune-data' / key).items() if k not in current}
            if missing:
                config.set_rune_configs(key, missing)
        except Exception:
            pass   # 못 채우면 사람이 설정 화면에서 넣는다 — 앱이 안 켜지는 것보다 낫다


def _rune_source(source):
    """폴더 안의 파일을 골랐으면 그 폴더다 — 고르기 창 하나로 zip 과 폴더를 둘 다 받기 위해서다."""
    source = Path(source)
    return source.parent if source.is_file() and source.suffix.lower() != '.zip' else source


def _who():
    """봉인한 사람. 로그인 이름을 못 얻어도 봉인은 진행한다 (vader.core.ui._who 와 같은 규칙)."""
    import getpass
    try:
        return getpass.getuser() or '알 수 없음'
    except Exception:
        return '알 수 없음'
