import gc
import importlib
import itertools
import os
import ssl
import sys
import typing
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone
from logging import LogRecord
from pickle import dumps, loads
from typing import Optional
from unittest.mock import ANY, DEFAULT, MagicMock, Mock, patch

import pytest
from kombu import Exchange, Queue
from pydantic import BaseModel, ValidationInfo, model_validator
from vine import promise

from celery import Celery, _state
from celery import app as _app
from celery import current_app, shared_task
from celery.app import base as _appbase
from celery.app import defaults
from celery.backends.base import Backend
from celery.contrib.testing.mocks import ContextMock
from celery.exceptions import ImproperlyConfigured
from celery.loaders.base import unconfigured
from celery.platforms import pyimplementation
from celery.utils.collections import DictAttribute
from celery.utils.objects import Bunch
from celery.utils.serialization import pickle
from celery.utils.time import localize, timezone, to_utc
from t.unit import conftest

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo  # noqa

THIS_IS_A_KEY = 'this is a value'


class ObjectConfig:
    FOO = 1
    BAR = 2


object_config = ObjectConfig()
dict_config = {'FOO': 10, 'BAR': 20}


class ObjectConfig2:
    LEAVE_FOR_WORK = True
    MOMENT_TO_STOP = True
    CALL_ME_BACK = 123456789
    WANT_ME_TO = False
    UNDERSTAND_ME = True


class test_module:

    def test_default_app(self):
        assert _app.default_app == _state.default_app

    def test_bugreport(self, app):
        assert _app.bugreport(app=app)


class test_task_join_will_block:

    def test_task_join_will_block(self, patching):
        patching('celery._state._task_join_will_block', 0)
        assert _state._task_join_will_block == 0
        _state._set_task_join_will_block(True)
        assert _state._task_join_will_block is True
        # fixture 'app' sets this, so need to use orig_ function
        # set there by that fixture.
        res = _state.orig_task_join_will_block()
        assert res is True


class test_App:

    def setup_method(self):
        self.app.add_defaults(deepcopy(self.CELERY_TEST_CONFIG))

    def test_now(self):
        timezone_setting_value = 'US/Eastern'
        tz_utc = timezone.get_timezone('UTC')
        tz_us_eastern = timezone.get_timezone(timezone_setting_value)

        now = to_utc(datetime.now(datetime_timezone.utc))
        app_now = self.app.now()

        assert app_now.tzinfo is tz_utc
        assert app_now - now <= timedelta(seconds=1)

        # Check that timezone conversion is applied from configuration
        self.app.conf.enable_utc = False
        self.app.conf.timezone = timezone_setting_value
        # timezone is a cached property
        del self.app.timezone

        app_now = self.app.now()

        assert app_now.tzinfo == tz_us_eastern

        diff = to_utc(datetime.now(datetime_timezone.utc)) - localize(app_now, tz_utc)
        assert diff <= timedelta(seconds=1)

        # Verify that timezone setting overrides enable_utc=on setting
        self.app.conf.enable_utc = True
        del self.app.timezone
        app_now = self.app.now()
        assert self.app.timezone == tz_us_eastern
        assert app_now.tzinfo == tz_us_eastern

    @patch('celery.app.base.set_default_app')
    def test_set_default(self, set_default_app):
        self.app.set_default()
        set_default_app.assert_called_with(self.app)

    @patch('celery.security.setup_security')
    def test_setup_security(self, setup_security):
        self.app.setup_security(
            {'json'}, 'key', None, 'cert', 'store', 'digest', 'serializer')
        setup_security.assert_called_with(
            {'json'}, 'key', None, 'cert', 'store', 'digest', 'serializer',
            app=self.app)

    def test_task_autofinalize_disabled(self):
        with self.Celery('xyzibari', autofinalize=False) as app:
            @app.task
            def ttafd():
                return 42

            with pytest.raises(RuntimeError):
                ttafd()

        with self.Celery('xyzibari', autofinalize=False) as app:
            @app.task
            def ttafd2():
                return 42

            app.finalize()
            assert ttafd2() == 42

    def test_registry_autofinalize_disabled(self):
        with self.Celery('xyzibari', autofinalize=False) as app:
            with pytest.raises(RuntimeError):
                app.tasks['celery.chain']
            app.finalize()
            assert app.tasks['celery.chain']

    def test_task(self):
        with self.Celery('foozibari') as app:

            def fun():
                pass

            fun.__module__ = '__main__'
            task = app.task(fun)
            assert task.name == app.main + '.fun'

    def test_task_too_many_args(self):
        with pytest.raises(TypeError):
            self.app.task(Mock(name='fun'), True)
        with pytest.raises(TypeError):
            self.app.task(Mock(name='fun'), True, 1, 2)

    def test_with_config_source(self):
        with self.Celery(config_source=ObjectConfig) as app:
            assert app.conf.FOO == 1
            assert app.conf.BAR == 2

    @pytest.mark.usefixtures('depends_on_current_app')
    def test_task_windows_execv(self):
        prev, _appbase.USING_EXECV = _appbase.USING_EXECV, True
        try:
            @self.app.task(shared=False)
            def foo():
                pass

            assert foo._get_current_object()  # is proxy

        finally:
            _appbase.USING_EXECV = prev
        assert not _appbase.USING_EXECV

    def test_task_takes_no_args(self):
        with pytest.raises(TypeError):
            @self.app.task(1)
            def foo():
                pass

    def test_add_defaults(self):
        assert not self.app.configured
        _conf = {'foo': 300}

        def conf():
            return _conf

        self.app.add_defaults(conf)
        assert conf in self.app._pending_defaults
        assert not self.app.configured
        assert self.app.conf.foo == 300
        assert self.app.configured
        assert not self.app._pending_defaults

        # defaults not pickled
        appr = loads(dumps(self.app))
        with pytest.raises(AttributeError):
            appr.conf.foo

        # add more defaults after configured
        conf2 = {'foo': 'BAR'}
        self.app.add_defaults(conf2)
        assert self.app.conf.foo == 'BAR'

        assert _conf in self.app.conf.defaults
        assert conf2 in self.app.conf.defaults

    def test_connection_or_acquire(self):
        with self.app.connection_or_acquire(block=True):
            assert self.app.pool._dirty

        with self.app.connection_or_acquire(pool=False):
            assert not self.app.pool._dirty

    def test_using_v1_reduce(self):
        self.app._using_v1_reduce = True
        assert loads(dumps(self.app))

    def test_autodiscover_tasks_force_fixup_fallback(self):
        self.app.loader.autodiscover_tasks = Mock()
        self.app.autodiscover_tasks([], force=True)
        self.app.loader.autodiscover_tasks.assert_called_with(
            [], 'tasks',
        )

    def test_autodiscover_tasks_force(self):
        self.app.loader.autodiscover_tasks = Mock()
        self.app.autodiscover_tasks(['proj.A', 'proj.B'], force=True)
        self.app.loader.autodiscover_tasks.assert_called_with(
            ['proj.A', 'proj.B'], 'tasks',
        )
        self.app.loader.autodiscover_tasks = Mock()

        def lazy_list():
            return ['proj.A', 'proj.B']
        self.app.autodiscover_tasks(
            lazy_list,
            related_name='george',
            force=True,
        )
        self.app.loader.autodiscover_tasks.assert_called_with(
            ['proj.A', 'proj.B'], 'george',
        )

    def test_autodiscover_tasks_lazy(self):
        with patch('celery.signals.import_modules') as import_modules:
            def lazy_list():
                return [1, 2, 3]
            self.app.autodiscover_tasks(lazy_list)
            import_modules.connect.assert_called()
            prom = import_modules.connect.call_args[0][0]
            assert isinstance(prom, promise)
            assert prom.fun == self.app._autodiscover_tasks
            assert prom.args[0](), [1, 2 == 3]

    def test_autodiscover_tasks__no_packages(self):
        fixup1 = Mock(name='fixup')
        fixup2 = Mock(name='fixup')
        self.app._autodiscover_tasks_from_names = Mock(name='auto')
        self.app._fixups = [fixup1, fixup2]
        fixup1.autodiscover_tasks.return_value = ['A', 'B', 'C']
        fixup2.autodiscover_tasks.return_value = ['D', 'E', 'F']
        self.app.autodiscover_tasks(force=True)
        self.app._autodiscover_tasks_from_names.assert_called_with(
            ['A', 'B', 'C', 'D', 'E', 'F'], related_name='tasks',
        )

    def test_with_broker(self, patching):
        patching.setenv('CELERY_BROKER_URL', '')
        with self.Celery(broker='foo://baribaz') as app:
            assert app.conf.broker_url == 'foo://baribaz'

    def test_pending_configuration_non_true__kwargs(self):
        with self.Celery(task_create_missing_queues=False) as app:
            assert app.conf.task_create_missing_queues is False

    def test_pending_configuration__kwargs(self):
        with self.Celery(foo='bar') as app:
            assert app.conf.foo == 'bar'

    def test_pending_configuration__setattr(self):
        with self.Celery(broker='foo://bar') as app:
            app.conf.task_default_delivery_mode = 44
            app.conf.worker_agent = 'foo:Bar'
            assert not app.configured
            assert app.conf.worker_agent == 'foo:Bar'
            assert app.conf.broker_url == 'foo://bar'
            assert app._preconf['worker_agent'] == 'foo:Bar'

            assert app.configured
            reapp = pickle.loads(pickle.dumps(app))
            assert reapp._preconf['worker_agent'] == 'foo:Bar'
            assert not reapp.configured
            assert reapp.conf.worker_agent == 'foo:Bar'
            assert reapp.configured
            assert reapp.conf.broker_url == 'foo://bar'
            assert reapp._preconf['worker_agent'] == 'foo:Bar'

    def test_pending_configuration__update(self):
        with self.Celery(broker='foo://bar') as app:
            app.conf.update(
                task_default_delivery_mode=44,
                worker_agent='foo:Bar',
            )
            assert not app.configured
            assert app.conf.worker_agent == 'foo:Bar'
            assert app.conf.broker_url == 'foo://bar'
            assert app._preconf['worker_agent'] == 'foo:Bar'

    def test_pending_configuration__compat_settings(self):
        with self.Celery(broker='foo://bar', backend='foo') as app:
            app.conf.update(
                CELERY_ALWAYS_EAGER=4,
                CELERY_DEFAULT_DELIVERY_MODE=63,
                CELERYD_AGENT='foo:Barz',
            )
            assert app.conf.task_always_eager == 4
            assert app.conf.task_default_delivery_mode == 63
            assert app.conf.worker_agent == 'foo:Barz'
            assert app.conf.broker_url == 'foo://bar'
            assert app.conf.result_backend == 'foo'

    def test_pending_configuration__compat_settings_mixing(self):
        with self.Celery(broker='foo://bar', backend='foo') as app:
            app.conf.update(
                CELERY_ALWAYS_EAGER=4,
                CELERY_DEFAULT_DELIVERY_MODE=63,
                CELERYD_AGENT='foo:Barz',
                worker_consumer='foo:Fooz',
            )
            with pytest.raises(ImproperlyConfigured):
                assert app.conf.task_always_eager == 4

    def test_pending_configuration__django_settings(self):
        with self.Celery(broker='foo://bar', backend='foo') as app:
            app.config_from_object(DictAttribute(Bunch(
                CELERY_TASK_ALWAYS_EAGER=4,
                CELERY_TASK_DEFAULT_DELIVERY_MODE=63,
                CELERY_WORKER_AGENT='foo:Barz',
                CELERY_RESULT_SERIALIZER='pickle',
            )), namespace='CELERY')
            assert app.conf.result_serializer == 'pickle'
            assert app.conf.CELERY_RESULT_SERIALIZER == 'pickle'
            assert app.conf.task_always_eager == 4
            assert app.conf.task_default_delivery_mode == 63
            assert app.conf.worker_agent == 'foo:Barz'
            assert app.conf.broker_url == 'foo://bar'
            assert app.conf.result_backend == 'foo'

    def test_pending_configuration__compat_settings_mixing_new(self):
        with self.Celery(broker='foo://bar', backend='foo') as app:
            app.conf.update(
                task_always_eager=4,
                task_default_delivery_mode=63,
                worker_agent='foo:Barz',
                CELERYD_CONSUMER='foo:Fooz',
                CELERYD_AUTOSCALER='foo:Xuzzy',
            )
            with pytest.raises(ImproperlyConfigured):
                assert app.conf.worker_consumer == 'foo:Fooz'

    def test_pending_configuration__compat_settings_mixing_alt(self):
        with self.Celery(broker='foo://bar', backend='foo') as app:
            app.conf.update(
                task_always_eager=4,
                task_default_delivery_mode=63,
                worker_agent='foo:Barz',
                CELERYD_CONSUMER='foo:Fooz',
                worker_consumer='foo:Fooz',
                CELERYD_AUTOSCALER='foo:Xuzzy',
                worker_autoscaler='foo:Xuzzy'
            )

    def test_pending_configuration__setdefault(self):
        with self.Celery(broker='foo://bar') as app:
            assert not app.configured
            app.conf.setdefault('worker_agent', 'foo:Bar')
            assert not app.configured

    def test_pending_configuration__iter(self):
        with self.Celery(broker='foo://bar') as app:
            app.conf.worker_agent = 'foo:Bar'
            assert not app.configured
            assert list(app.conf.keys())
            assert app.configured
            assert 'worker_agent' in app.conf
            assert dict(app.conf)

    def test_pending_configuration__raises_ImproperlyConfigured(self):
        with self.Celery(set_as_current=False) as app:
            app.conf.worker_agent = 'foo://bar'
            app.conf.task_default_delivery_mode = 44
            app.conf.CELERY_ALWAYS_EAGER = 5
            with pytest.raises(ImproperlyConfigured):
                app.finalize()

        with self.Celery() as app:
            assert not self.app.conf.task_always_eager

    def test_pending_configuration__ssl_settings(self):
        with self.Celery(broker='foo://bar',
                         broker_use_ssl={
                             'ssl_cert_reqs': ssl.CERT_REQUIRED,
                             'ssl_ca_certs': '/path/to/ca.crt',
                             'ssl_certfile': '/path/to/client.crt',
                             'ssl_keyfile': '/path/to/client.key'},
                         redis_backend_use_ssl={
                             'ssl_cert_reqs': ssl.CERT_REQUIRED,
                             'ssl_ca_certs': '/path/to/ca.crt',
                             'ssl_certfile': '/path/to/client.crt',
                             'ssl_keyfile': '/path/to/client.key'}) as app:
            assert not app.configured
            assert app.conf.broker_url == 'foo://bar'
            assert app.conf.broker_use_ssl['ssl_certfile'] == \
                '/path/to/client.crt'
            assert app.conf.broker_use_ssl['ssl_keyfile'] == \
                '/path/to/client.key'
            assert app.conf.broker_use_ssl['ssl_ca_certs'] == \
                '/path/to/ca.crt'
            assert app.conf.broker_use_ssl['ssl_cert_reqs'] == \
                ssl.CERT_REQUIRED
            assert app.conf.redis_backend_use_ssl['ssl_certfile'] == \
                '/path/to/client.crt'
            assert app.conf.redis_backend_use_ssl['ssl_keyfile'] == \
                '/path/to/client.key'
            assert app.conf.redis_backend_use_ssl['ssl_ca_certs'] == \
                '/path/to/ca.crt'
            assert app.conf.redis_backend_use_ssl['ssl_cert_reqs'] == \
                ssl.CERT_REQUIRED

    def test_repr(self):
        assert repr(self.app)

    def test_custom_task_registry(self):
        with self.Celery(tasks=self.app.tasks) as app2:
            assert app2.tasks is self.app.tasks

    def test_include_argument(self):
        with self.Celery(include=('foo', 'bar.foo')) as app:
            assert app.conf.include, ('foo' == 'bar.foo')

    def test_set_as_current(self):
        current = _state._tls.current_app
        try:
            app = self.Celery(set_as_current=True)
            assert _state._tls.current_app is app
        finally:
            _state._tls.current_app = current

    def test_current_task(self):
        @self.app.task
        def foo(shared=False):
            pass

        _state._task_stack.push(foo)
        try:
            assert self.app.current_task.name == foo.name
        finally:
            _state._task_stack.pop()

    def test_task_not_shared(self):
        with patch('celery.app.base.connect_on_app_finalize') as sh:
            @self.app.task(shared=False)
            def foo():
                pass
            sh.assert_not_called()

    def test_task_compat_with_filter(self):
        with self.Celery() as app:
            check = Mock()

            def filter(task):
                check(task)
                return task

            @app.task(filter=filter, shared=False)
            def foo():
                pass
            check.assert_called_with(foo)

    def test_task_with_filter(self):
        with self.Celery() as app:
            check = Mock()

            def filter(task):
                check(task)
                return task

            assert not _appbase.USING_EXECV

            @app.task(filter=filter, shared=False)
            def foo():
                pass
            check.assert_called_with(foo)

    def test_task_with_pydantic_with_no_args(self):
        """Test a pydantic task with no arguments or return value."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo():
                check()

            assert foo() is None
            check.assert_called_once()

    def test_task_with_pydantic_with_arg_and_kwarg(self):
        """Test a pydantic task with simple (non-pydantic) arg/kwarg and return value."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: int, kwarg: bool = True) -> int:
                check(arg, kwarg=kwarg)
                return 1

            assert foo(0) == 1
            check.assert_called_once_with(0, kwarg=True)

    def test_task_with_pydantic_with_optional_args(self):
        """Test pydantic task receiving and returning an optional argument."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: Optional[int], kwarg: Optional[bool] = True) -> Optional[int]:
                check(arg, kwarg=kwarg)
                if isinstance(arg, int):
                    return 1
                return 2

            assert foo(0) == 1
            check.assert_called_once_with(0, kwarg=True)

            assert foo(None) == 2
            check.assert_called_with(None, kwarg=True)

    @pytest.mark.skipif(sys.version_info < (3, 9), reason="Notation is only supported in Python 3.9 or newer.")
    def test_task_with_pydantic_with_dict_args(self):
        """Test pydantic task receiving and returning a generic dict argument."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: dict[str, str], kwarg: dict[str, str]) -> dict[str, str]:
                check(arg, kwarg=kwarg)
                return {'x': 'y'}

            assert foo({'a': 'b'}, kwarg={'c': 'd'}) == {'x': 'y'}
            check.assert_called_once_with({'a': 'b'}, kwarg={'c': 'd'})

    @pytest.mark.skipif(sys.version_info < (3, 9), reason="Notation is only supported in Python 3.9 or newer.")
    def test_task_with_pydantic_with_list_args(self):
        """Test pydantic task receiving and returning a generic dict argument."""
        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: list[str], kwarg: list[str] = True) -> list[str]:
                check(arg, kwarg=kwarg)
                return ['x']

            assert foo(['a'], kwarg=['b']) == ['x']
            check.assert_called_once_with(['a'], kwarg=['b'])

    def test_task_with_pydantic_with_pydantic_arg_and_default_kwarg(self):
        """Test a pydantic task with pydantic arg/kwarg and return value."""

        class ArgModel(BaseModel):
            arg_value: int

        class KwargModel(BaseModel):
            kwarg_value: int

        kwarg_default = KwargModel(kwarg_value=1)

        class ReturnModel(BaseModel):
            ret_value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: ArgModel, kwarg: KwargModel = kwarg_default) -> ReturnModel:
                check(arg, kwarg=kwarg)
                return ReturnModel(ret_value=2)

            assert foo({'arg_value': 0}) == {'ret_value': 2}
            check.assert_called_once_with(ArgModel(arg_value=0), kwarg=kwarg_default)
            check.reset_mock()

            # Explicitly pass kwarg (but as argument)
            assert foo({'arg_value': 3}, {'kwarg_value': 4}) == {'ret_value': 2}
            check.assert_called_once_with(ArgModel(arg_value=3), kwarg=KwargModel(kwarg_value=4))
            check.reset_mock()

            # Explicitly pass all arguments as kwarg
            assert foo(arg={'arg_value': 5}, kwarg={'kwarg_value': 6}) == {'ret_value': 2}
            check.assert_called_once_with(ArgModel(arg_value=5), kwarg=KwargModel(kwarg_value=6))

    def test_task_with_pydantic_with_non_strict_validation(self):
        """Test a pydantic task with where Pydantic has to apply non-strict validation."""

        class Model(BaseModel):
            value: timedelta

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: Model) -> Model:
                check(arg)
                return Model(value=timedelta(days=arg.value.days * 2))

            assert foo({'value': timedelta(days=1)}) == {'value': 'P2D'}
            check.assert_called_once_with(Model(value=timedelta(days=1)))
            check.reset_mock()

            # Pass a serialized value to the task
            assert foo({'value': 'P3D'}) == {'value': 'P6D'}
            check.assert_called_once_with(Model(value=timedelta(days=3)))

    def test_task_with_pydantic_with_optional_pydantic_args(self):
        """Test pydantic task receiving and returning an optional argument."""
        class ArgModel(BaseModel):
            arg_value: int

        class KwargModel(BaseModel):
            kwarg_value: int

        class ReturnModel(BaseModel):
            ret_value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo(arg: Optional[ArgModel], kwarg: Optional[KwargModel] = None) -> Optional[ReturnModel]:
                check(arg, kwarg=kwarg)
                if isinstance(arg, ArgModel):
                    return ReturnModel(ret_value=1)
                return None

            assert foo(None) is None
            check.assert_called_once_with(None, kwarg=None)

            assert foo({'arg_value': 1}, kwarg={'kwarg_value': 2}) == {'ret_value': 1}
            check.assert_called_with(ArgModel(arg_value=1), kwarg=KwargModel(kwarg_value=2))

    @pytest.mark.skipif(sys.version_info < (3, 9), reason="Notation is only supported in Python 3.9 or newer.")
    def test_task_with_pydantic_with_generic_return_value(self):
        """Test pydantic task receiving and returning an optional argument."""
        class ReturnModel(BaseModel):
            ret_value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def foo() -> dict[str, str]:
                check()
                return ReturnModel(ret_value=1)  # type: ignore  # whole point here is that this doesn't match

            assert foo() == ReturnModel(ret_value=1)
            check.assert_called_once_with()

    def test_task_with_pydantic_with_task_name_in_context(self):
        """Test that the task name is passed to as additional context."""

        class ArgModel(BaseModel):
            value: int

            @model_validator(mode='after')
            def validate_context(self, info: ValidationInfo):
                context = info.context
                assert context
                assert context.get('celery_task_name') == 't.unit.app.test_app.task'
                return self

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True)
            def task(arg: ArgModel):
                check(arg)
                return 1

            assert task({'value': 1}) == 1

    def test_task_with_pydantic_with_strict_validation(self):
        """Test a pydantic task with/without strict model validation."""

        class ArgModel(BaseModel):
            value: int

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True, pydantic_strict=True)
            def strict(arg: ArgModel):
                check(arg)

            @app.task(pydantic=True, pydantic_strict=False)
            def loose(arg: ArgModel):
                check(arg)

            # In Pydantic, passing an "exact int" as float works without strict validation
            assert loose({'value': 1.0}) is None
            check.assert_called_once_with(ArgModel(value=1))
            check.reset_mock()

            # ... but a non-strict value will raise an exception
            with pytest.raises(ValueError):
                loose({'value': 1.1})
            check.assert_not_called()

            # ... with strict validation, even an "exact int" will not work:
            with pytest.raises(ValueError):
                strict({'value': 1.0})
            check.assert_not_called()

    def test_task_with_pydantic_with_extra_context(self):
        """Test passing additional validation context to the model."""

        class ArgModel(BaseModel):
            value: int

            @model_validator(mode='after')
            def validate_context(self, info: ValidationInfo):
                context = info.context
                assert context, context
                assert context.get('foo') == 'bar'
                return self

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True, pydantic_context={'foo': 'bar'})
            def task(arg: ArgModel):
                check(arg.value)
                return 1

            assert task({'value': 1}) == 1
            check.assert_called_once_with(1)

    def test_task_with_pydantic_with_dump_kwargs(self):
        """Test passing keyword arguments to model_dump()."""

        class ArgModel(BaseModel):
            value: int

        class RetModel(BaseModel):
            value: datetime
            unset_value: typing.Optional[int] = 99  # this would be in the output, if exclude_unset weren't True

        with self.Celery() as app:
            check = Mock()

            @app.task(pydantic=True, pydantic_dump_kwargs={'mode': 'python', 'exclude_unset': True})
            def task(arg: ArgModel) -> RetModel:
                check(arg)
                return RetModel(value=datetime(2024, 5, 14, tzinfo=timezone.utc))

            assert task({'value': 1}) == {'value': datetime(2024, 5, 14, tzinfo=timezone.utc)}
            check.assert_called_once_with(ArgModel(value=1))

    def test_task_with_pydantic_with_pydantic_not_installed(self):
        """Test configuring a task with Pydantic when pydantic is not installed."""

        with self.Celery() as app:
            @app.task(pydantic=True)
            def task():
                return

            # mock function will raise ModuleNotFoundError only if pydantic is imported
            def import_module(name, *args, **kwargs):
                if name == 'pydantic':
                    raise ModuleNotFoundError('Module not found.')
                return DEFAULT

            msg = r'^You need to install pydantic to use pydantic model serialization\.$'
            with patch(
                'celery.app.base.importlib.import_module',
                side_effect=import_module,
                wraps=importlib.import_module
            ):
                with pytest.raises(ImproperlyConfigured, match=msg):
                    task()

    def test_task_sets_main_name_MP_MAIN_FILE(self):
        from celery.utils import imports as _imports
        _imports.MP_MAIN_FILE = __file__
        try:
            with self.Celery('xuzzy') as app:

                @app.task
                def foo():
                    pass

                assert foo.name == 'xuzzy.foo'
        finally:
            _imports.MP_MAIN_FILE = None

    def test_can_get_type_hints_for_tasks(self):
        import typing

        with self.Celery() as app:
            @app.task
            def foo(parameter: int) -> None:
                pass

            assert typing.get_type_hints(foo) == {
                'parameter': int, 'return': type(None)}

    def test_annotate_decorator(self):
        from celery.app.task import Task

        class adX(Task):

            def run(self, y, z, x):
                return y, z, x

        check = Mock()

        def deco(fun):

            def _inner(*args, **kwargs):
                check(*args, **kwargs)
                return fun(*args, **kwargs)
            return _inner

        self.app.conf.task_annotations = {
            adX.name: {'@__call__': deco}
        }
        adX.bind(self.app)
        assert adX.app is self.app

        i = adX()
        i(2, 4, x=3)
        check.assert_called_with(i, 2, 4, x=3)

        i.annotate()
        i.annotate()

    def test_apply_async_adds_children(self):
        from celery._state import _task_stack

        @self.app.task(bind=True, shared=False)
        def a3cX1(self):
            pass

        @self.app.task(bind=True, shared=False)
        def a3cX2(self):
            pass

        _task_stack.push(a3cX1)
        try:
            a3cX1.push_request(called_directly=False)
            try:
                res = a3cX2.apply_async(add_to_parent=True)
                assert res in a3cX1.request.children
            finally:
                a3cX1.pop_request()
        finally:
            _task_stack.pop()

    def test_pickle_app(self):
        changes = {'THE_FOO_BAR': 'bars',
                   'THE_MII_MAR': 'jars'}
        self.app.conf.update(changes)
        saved = pickle.dumps(self.app)
        assert len(saved) < 2048
        restored = pickle.loads(saved)
        for key, value in changes.items():
            assert restored.conf[key] == value

    @patch('celery.bin.celery.celery')
    def test_worker_main(self, mocked_celery):
        self.app.worker_main(argv=['worker', '--help'])

        mocked_celery.main.assert_called_with(
            args=['worker', '--help'], standalone_mode=False)

    def test_config_from_envvar(self, monkeypatch):
        monkeypatch.setenv("CELERYTEST_CONFIG_OBJECT", 't.unit.app.test_app')
        self.app.config_from_envvar('CELERYTEST_CONFIG_OBJECT')
        assert self.app.conf.THIS_IS_A_KEY == 'this is a value'

    def assert_config2(self):
        assert self.app.conf.LEAVE_FOR_WORK
        assert self.app.conf.MOMENT_TO_STOP
        assert self.app.conf.CALL_ME_BACK == 123456789
        assert not self.app.conf.WANT_ME_TO
        assert self.app.conf.UNDERSTAND_ME

    def test_config_from_object__lazy(self):
        conf = ObjectConfig2()
        self.app.config_from_object(conf)
        assert self.app.loader._conf is unconfigured
        assert self.app._config_source is conf

        self.assert_config2()

    def test_config_from_object__force(self):
        self.app.config_from_object(ObjectConfig2(), force=True)
        assert self.app.loader._conf

        self.assert_config2()

    def test_config_from_object__compat(self):

        class Config:
            CELERY_ALWAYS_EAGER = 44
            CELERY_DEFAULT_DELIVERY_MODE = 30
            CELERY_TASK_PUBLISH_RETRY = False

        self.app.config_from_object(Config)
        assert self.app.conf.task_always_eager == 44
        assert self.app.conf.CELERY_ALWAYS_EAGER == 44
        assert not self.app.conf.task_publish_retry
        assert self.app.conf.task_default_routing_key == 'testcelery'

    def test_config_from_object__supports_old_names(self):

        class Config:
            task_always_eager = 45
            task_default_delivery_mode = 301

        self.app.config_from_object(Config())
        assert self.app.conf.CELERY_ALWAYS_EAGER == 45
        assert self.app.conf.task_always_eager == 45
        assert self.app.conf.CELERY_DEFAULT_DELIVERY_MODE == 301
        assert self.app.conf.task_default_delivery_mode == 301
        assert self.app.conf.task_default_routing_key == 'testcelery'

    def test_config_from_object__namespace_uppercase(self):

        class Config:
            CELERY_TASK_ALWAYS_EAGER = 44
            CELERY_TASK_DEFAULT_DELIVERY_MODE = 301

        self.app.config_from_object(Config(), namespace='CELERY')
        assert self.app.conf.task_always_eager == 44

    def test_config_from_object__namespace_lowercase(self):

        class Config:
            celery_task_always_eager = 44
            celery_task_default_delivery_mode = 301

        self.app.config_from_object(Config(), namespace='celery')
        assert self.app.conf.task_always_eager == 44

    def test_config_from_object__mixing_new_and_old(self):

        class Config:
            task_always_eager = 44
            worker_agent = 'foo:Agent'
            worker_consumer = 'foo:Consumer'
            beat_schedule = '/foo/schedule'
            CELERY_DEFAULT_DELIVERY_MODE = 301

        with pytest.raises(ImproperlyConfigured) as exc:
            self.app.config_from_object(Config(), force=True)
            assert exc.args[0].startswith('CELERY_DEFAULT_DELIVERY_MODE')
            assert 'task_default_delivery_mode' in exc.args[0]

    def test_config_from_object__mixing_old_and_new(self):

        class Config:
            CELERY_ALWAYS_EAGER = 46
            CELERYD_AGENT = 'foo:Agent'
            CELERYD_CONSUMER = 'foo:Consumer'
            CELERYBEAT_SCHEDULE = '/foo/schedule'
            task_default_delivery_mode = 301

        with pytest.raises(ImproperlyConfigured) as exc:
            self.app.config_from_object(Config(), force=True)
            assert exc.args[0].startswith('task_default_delivery_mode')
            assert 'CELERY_DEFAULT_DELIVERY_MODE' in exc.args[0]

    def test_config_form_object__module_attr_does_not_exist(self):
        module_name = __name__
        attr_name = 'bar'
        # the module must exist, but it should not have the config attr
        self.app.config_from_object(f'{module_name}.{attr_name}')

        with pytest.raises(ModuleNotFoundError) as exc:
            assert self.app.conf.broker_url is None

        assert module_name in exc.value.args[0]
        assert attr_name in exc.value.args[0]

    def test_config_from_cmdline(self):
        cmdline = ['task_always_eager=no',
                   'result_backend=/dev/null',
                   'worker_prefetch_multiplier=368',
                   '.foobarstring=(string)300',
                   '.foobarint=(int)300',
                   'database_engine_options=(dict){"foo": "bar"}']
        self.app.config_from_cmdline(cmdline, namespace='worker')
        assert not self.app.conf.task_always_eager
        assert self.app.conf.result_backend == '/dev/null'
        assert self.app.conf.worker_prefetch_multiplier == 368
        assert self.app.conf.worker_foobarstring == '300'
        assert self.app.conf.worker_foobarint == 300
        assert self.app.conf.database_engine_options == {'foo': 'bar'}

    def test_setting__broker_transport_options(self):

        _args = {'foo': 'bar', 'spam': 'baz'}

        self.app.config_from_object(Bunch())
        assert self.app.conf.broker_transport_options == \
            {'polling_interval': 0.1}

        self.app.config_from_object(Bunch(broker_transport_options=_args))
        assert self.app.conf.broker_transport_options == _args

    def test_Windows_log_color_disabled(self):
        self.app.IS_WINDOWS = True
        assert not self.app.log.supports_color(True)

    def test_WorkController(self):
        x = self.app.WorkController
        assert x.app is self.app

    def test_Worker(self):
        x = self.app.Worker
        assert x.app is self.app

    @pytest.mark.usefixtures('depends_on_current_app')
    def test_AsyncResult(self):
        x = self.app.AsyncResult('1')
        assert x.app is self.app
        r = loads(dumps(x))
        # not set as current, so ends up as default app after reduce
        assert r.app is current_app._get_current_object()

    def test_get_active_apps(self):
        assert list(_state._get_active_apps())

        app1 = self.Celery()
        appid = id(app1)
        assert app1 in _state._get_active_apps()
        app1.close()
        del (app1)

        gc.collect()

        # weakref removed from list when app goes out of scope.
        with pytest.raises(StopIteration):
            next(app for app in _state._get_active_apps() if id(app) == appid)

    def test_config_from_envvar_more(self, key='CELERY_HARNESS_CFG1'):
        assert not self.app.config_from_envvar(
            'HDSAJIHWIQHEWQU', force=True, silent=True)
        with pytest.raises(ImproperlyConfigured):
            self.app.config_from_envvar(
                'HDSAJIHWIQHEWQU', force=True, silent=False,
            )
        os.environ[key] = __name__ + '.object_config'
        assert self.app.config_from_envvar(key, force=True)
        assert self.app.conf['FOO'] == 1
        assert self.app.conf['BAR'] == 2

        os.environ[key] = 'unknown_asdwqe.asdwqewqe'
        with pytest.raises(ImportError):
            self.app.config_from_envvar(key, silent=False)
        assert not self.app.config_from_envvar(key, force=True, silent=True)

        os.environ[key] = __name__ + '.dict_config'
        assert self.app.config_from_envvar(key, force=True)
        assert self.app.conf['FOO'] == 10
        assert self.app.conf['BAR'] == 20

    @patch('celery.bin.celery.celery')
    def test_start(self, mocked_celery):
        self.app.start()
        mocked_celery.main.assert_called()

    @pytest.mark.parametrize('url,expected_fields', [
        ('pyamqp://', {
            'hostname': 'localhost',
            'userid': 'guest',
            'password': 'guest',
            'virtual_host': '/',
        }),
        ('pyamqp://:1978/foo', {
            'port': 1978,
            'virtual_host': 'foo',
        }),
        ('pyamqp:////value', {
            'virtual_host': '/value',
        })
    ])
    def test_amqp_get_broker_info(self, url, expected_fields):
        info = self.app.connection(url).info()
        for key, expected_value in expected_fields.items():
            assert info[key] == expected_value

    def test_amqp_failover_strategy_selection(self):
        # Test passing in a string and make sure the string
        # gets there untouched
        self.app.conf.broker_failover_strategy = 'foo-bar'
        assert self.app.connection('amqp:////value') \
                       .failover_strategy == 'foo-bar'

        # Try passing in None
        self.app.conf.broker_failover_strategy = None
        assert self.app.connection('amqp:////value') \
                       .failover_strategy == itertools.cycle

        # Test passing in a method
        def my_failover_strategy(it):
            yield True

        self.app.conf.broker_failover_strategy = my_failover_strategy
        assert self.app.connection('amqp:////value') \
                       .failover_strategy == my_failover_strategy

    def test_after_fork(self):
        self.app._pool = Mock()
        self.app.on_after_fork = Mock(name='on_after_fork')
        self.app._after_fork()
        assert self.app._pool is None
        self.app.on_after_fork.send.assert_called_with(sender=self.app)
        self.app._after_fork()

    def test_global_after_fork(self):
        self.app._after_fork = Mock(name='_after_fork')
        _appbase._after_fork_cleanup_app(self.app)
        self.app._after_fork.assert_called_with()

    @patch('celery.app.base.logger')
    def test_after_fork_cleanup_app__raises(self, logger):
        self.app._after_fork = Mock(name='_after_fork')
        exc = self.app._after_fork.side_effect = KeyError()
        _appbase._after_fork_cleanup_app(self.app)
        logger.info.assert_called_with(
            'after forker raised exception: %r', exc, exc_info=1)

    def test_ensure_after_fork__no_multiprocessing(self):
        prev, _appbase.register_after_fork = (
            _appbase.register_after_fork, None)
        try:
            self.app._after_fork_registered = False
            self.app._ensure_after_fork()
            assert self.app._after_fork_registered
        finally:
            _appbase.register_after_fork = prev

    def test_canvas(self):
        assert self.app._canvas.Signature

    def test_signature(self):
        sig = self.app.signature('foo', (1, 2))
        assert sig.app is self.app

    def test_timezone__none_set(self):
        self.app.conf.timezone = None
        self.app.conf.enable_utc = True
        assert self.app.timezone == timezone.utc
        del self.app.timezone
        self.app.conf.enable_utc = False
        assert self.app.timezone == timezone.local

    def test_uses_utc_timezone(self):
        self.app.conf.timezone = None
        self.app.conf.enable_utc = True
        assert self.app.uses_utc_timezone() is True

        self.app.conf.enable_utc = False
        del self.app.timezone
        assert self.app.uses_utc_timezone() is False

        self.app.conf.timezone = 'US/Eastern'
        del self.app.timezone
        assert self.app.uses_utc_timezone() is False

        self.app.conf.timezone = 'UTC'
        del self.app.timezone
        assert self.app.uses_utc_timezone() is True

    def test_compat_on_configure(self):
        _on_configure = Mock(name='on_configure')

        class CompatApp(Celery):

            def on_configure(self, *args, **kwargs):
                # on pypy3 if named on_configure the class function
                # will be called, instead of the mock defined above,
                # so we add the underscore.
                _on_configure(*args, **kwargs)

        with CompatApp(set_as_current=False) as app:
            app.loader = Mock()
            app.loader.conf = {}
            app._load_config()
            _on_configure.assert_called_with()

    def test_add_periodic_task(self):

        @self.app.task
        def add(x, y):
            pass
        assert not self.app.configured
        self.app.add_periodic_task(
            10, self.app.signature('add', (2, 2)),
            name='add1', expires=3,
        )
        assert self.app._pending_periodic_tasks
        assert not self.app.configured

        sig2 = add.s(4, 4)
        assert self.app.configured
        self.app.add_periodic_task(20, sig2, name='add2', expires=4)
        assert 'add1' in self.app.conf.beat_schedule
        assert 'add2' in self.app.conf.beat_schedule

    def test_add_periodic_task_expected_override(self):

        @self.app.task
        def add(x, y):
            pass
        sig = add.s(2, 2)
        self.app.add_periodic_task(10, sig, name='add1', expires=3)
        self.app.add_periodic_task(20, sig, name='add1', expires=3)
        assert 'add1' in self.app.conf.beat_schedule
        assert len(self.app.conf.beat_schedule) == 1

    def test_add_periodic_task_unexpected_override(self, caplog):

        @self.app.task
        def add(x, y):
            pass
        sig = add.s(2, 2)
        self.app.add_periodic_task(10, sig, expires=3)
        self.app.add_periodic_task(20, sig, expires=3)

        assert len(self.app.conf.beat_schedule) == 1
        assert caplog.records[0].message == (
            "Periodic task key='t.unit.app.test_app.add(2, 2)' shadowed a"
            " previous unnamed periodic task. Pass a name kwarg to"
            " add_periodic_task to silence this warning."
        )

    @pytest.mark.masked_modules('multiprocessing.util')
    def test_pool_no_multiprocessing(self, mask_modules):
        pool = self.app.pool
        assert pool is self.app._pool

    def test_bugreport(self):
        assert self.app.bugreport()

    @patch('celery.app.base.detect_quorum_queues', return_value=[False, ""])
    def test_send_task__connection_provided(self, detect_quorum_queues):
        connection = Mock(name='connection')
        router = Mock(name='router')
        router.route.return_value = {}
        self.app.amqp = Mock(name='amqp')
        self.app.amqp.Producer.attach_mock(ContextMock(), 'return_value')
        self.app.send_task('foo', (1, 2), connection=connection, router=router)
        self.app.amqp.Producer.assert_called_with(
            connection, auto_declare=False)
        self.app.amqp.send_task_message.assert_called_with(
            self.app.amqp.Producer(), 'foo',
            self.app.amqp.create_task_message())

    def test_send_task_sent_event(self):

        class Dispatcher:
            sent = []

            def publish(self, type, fields, *args, **kwargs):
                self.sent.append((type, fields))

        conn = self.app.connection()
        chan = conn.channel()
        try:
            for e in ('foo_exchange', 'moo_exchange', 'bar_exchange'):
                chan.exchange_declare(e, 'direct', durable=True)
                chan.queue_declare(e, durable=True)
                chan.queue_bind(e, e, e)
        finally:
            chan.close()
        assert conn.transport_cls == 'memory'

        message = self.app.amqp.create_task_message(
            'id', 'footask', (), {}, create_sent_event=True,
        )

        prod = self.app.amqp.Producer(conn)
        dispatcher = Dispatcher()
        self.app.amqp.send_task_message(
            prod, 'footask', message,
            exchange='moo_exchange', routing_key='moo_exchange',
            event_dispatcher=dispatcher,
        )
        assert dispatcher.sent
        assert dispatcher.sent[0][0] == 'task-sent'
        self.app.amqp.send_task_message(
            prod, 'footask', message, event_dispatcher=dispatcher,
            exchange='bar_exchange', routing_key='bar_exchange',
        )

    def test_select_queues(self):
        self.app.amqp = Mock(name='amqp')
        self.app.select_queues({'foo', 'bar'})
        self.app.amqp.queues.select.assert_called_with({'foo', 'bar'})

    def test_Beat(self):
        from celery.apps.beat import Beat
        beat = self.app.Beat()
        assert isinstance(beat, Beat)

    def test_registry_cls(self):

        class TaskRegistry(self.app.registry_cls):
            pass

        class CustomCelery(type(self.app)):
            registry_cls = TaskRegistry

        app = CustomCelery(set_as_current=False)
        assert isinstance(app.tasks, TaskRegistry)

    def test_oid(self):
        # Test that oid is global value.
        oid1 = self.app.oid
        oid2 = self.app.oid
        uuid.UUID(oid1)
        uuid.UUID(oid2)
        assert oid1 == oid2

    def test_global_oid(self):
        # Test that oid is global value also within threads
        main_oid = self.app.oid
        uuid.UUID(main_oid)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: self.app.oid)
        thread_oid = future.result()
        uuid.UUID(thread_oid)
        assert main_oid == thread_oid

    def test_thread_oid(self):
        # Test that thread_oid is global value in single thread.
        oid1 = self.app.thread_oid
        oid2 = self.app.thread_oid
        uuid.UUID(oid1)
        uuid.UUID(oid2)
        assert oid1 == oid2

    def test_backend(self):
        # Test that app.backend returns the same backend in single thread
        backend1 = self.app.backend
        backend2 = self.app.backend
        assert isinstance(backend1, Backend)
        assert isinstance(backend2, Backend)
        assert backend1 is backend2

    def test_thread_backend(self):
        # Test that app.backend returns the new backend for each thread
        main_backend = self.app.backend
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: self.app.backend)
        thread_backend = future.result()
        assert isinstance(main_backend, Backend)
        assert isinstance(thread_backend, Backend)
        assert main_backend is not thread_backend

    def test_thread_oid_is_local(self):
        # Test that thread_oid is local to thread.
        main_oid = self.app.thread_oid
        uuid.UUID(main_oid)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: self.app.thread_oid)
        thread_oid = future.result()
        uuid.UUID(thread_oid)
        assert main_oid != thread_oid

    def test_thread_backend_thread_safe(self):
        # Should share the backend object across threads
        from concurrent.futures import ThreadPoolExecutor

        with self.Celery() as app:
            app.conf.update(result_backend_thread_safe=True)
            main_backend = app.backend
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: app.backend)

            thread_backend = future.result()
            assert isinstance(main_backend, Backend)
            assert isinstance(thread_backend, Backend)
            assert main_backend is thread_backend

    def test_send_task_expire_as_string(self):
        try:
            self.app.send_task(
                'foo', (1, 2),
                expires='2023-03-16T17:21:20.663973')
        except TypeError as e:
            pytest.fail(f'raise unexcepted error {e}')

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery_countdown(self, detect_quorum_queues):
        self.app.amqp = MagicMock(name='amqp')
        self.app.amqp.router.route.return_value = {
            'queue': Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='topic')
            )
        }

        self.app.send_task('foo', (1, 2), countdown=30)

        exchange = Exchange(
            'celery_delayed_27',
            type='topic',
        )
        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            exchange=exchange,
            routing_key='0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.1.1.1.1.0.testcelery'
        )
        driver_type_stub = self.app.amqp.producer_pool.connections.connection.transport.driver_type
        detect_quorum_queues.assert_called_once_with(self.app, driver_type_stub)

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery__no_queue_arg__no_eta(self, detect_quorum_queues):
        self.app.amqp = MagicMock(name='amqp')
        options = {
            'routing_key': 'testcelery',
            'exchange': 'testcelery',
            'exchange_type': 'topic',
        }
        self.app.amqp.router.route.return_value = options

        self.app.send_task(
            name='foo',
            args=(1, 2),
        )
        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            **options,
        )
        assert not detect_quorum_queues.called

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery__no_queue_arg__with_countdown(self, detect_quorum_queues):
        self.app.amqp = MagicMock(name='amqp')
        options = {
            'routing_key': 'testcelery',
            'exchange': 'testcelery',
            'exchange_type': 'topic',
        }
        self.app.amqp.router.route.return_value = options

        self.app.send_task(
            name='foo',
            args=(1, 2),
            countdown=30,
        )
        exchange = Exchange(
            'celery_delayed_27',
            type='topic',
        )
        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            exchange=exchange,
            routing_key='0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.1.1.1.1.0.testcelery',
            exchange_type="topic",
        )
        driver_type_stub = self.app.amqp.producer_pool.connections.connection.transport.driver_type
        detect_quorum_queues.assert_called_once_with(self.app, driver_type_stub)

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery_eta_datetime(self, detect_quorum_queues):
        self.app.amqp = MagicMock(name='amqp')
        self.app.amqp.router.route.return_value = {
            'queue': Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='topic')
            )
        }
        self.app.now = Mock(return_value=datetime(2024, 8, 24, tzinfo=datetime_timezone.utc))

        self.app.send_task('foo', (1, 2), eta=datetime(2024, 8, 25))

        exchange = Exchange(
            'celery_delayed_27',
            type='topic',
        )
        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            exchange=exchange,
            routing_key='0.0.0.0.0.0.0.0.0.0.0.1.0.1.0.1.0.0.0.1.1.0.0.0.0.0.0.0.testcelery'
        )

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery_eta_str(self, detect_quorum_queues):
        self.app.amqp = MagicMock(name='amqp')
        self.app.amqp.router.route.return_value = {
            'queue': Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='topic')
            )
        }
        self.app.now = Mock(return_value=datetime(2024, 8, 24, tzinfo=datetime_timezone.utc))

        self.app.send_task('foo', (1, 2), eta=datetime(2024, 8, 25).isoformat())

        exchange = Exchange(
            'celery_delayed_27',
            type='topic',
        )
        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            exchange=exchange,
            routing_key='0.0.0.0.0.0.0.0.0.0.0.1.0.1.0.1.0.0.0.1.1.0.0.0.0.0.0.0.testcelery',
        )

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery_no_eta_or_countdown(self, detect_quorum_queues):
        self.app.amqp = MagicMock(name='amqp')
        self.app.amqp.router.route.return_value = {'queue': Queue('testcelery', routing_key='testcelery')}

        self.app.send_task('foo', (1, 2), countdown=-10)

        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            queue=Queue(
                'testcelery',
                routing_key='testcelery'
            )
        )

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery_countdown_in_the_past(self, detect_quorum_queues):
        self.app.amqp = MagicMock(name='amqp')
        self.app.amqp.router.route.return_value = {
            'queue': Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='topic')
            )
        }

        self.app.send_task('foo', (1, 2))

        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            queue=Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='topic')
            )
        )

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery_eta_in_the_past(self, detect_quorum_queues):
        self.app.amqp = MagicMock(name='amqp')
        self.app.amqp.router.route.return_value = {
            'queue': Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='topic')
            )
        }
        self.app.now = Mock(return_value=datetime(2024, 8, 24, tzinfo=datetime_timezone.utc))

        self.app.send_task('foo', (1, 2), eta=datetime(2024, 8, 23).isoformat())

        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            queue=Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='topic')
            )
        )

    @patch('celery.app.base.detect_quorum_queues', return_value=[True, "testcelery"])
    def test_native_delayed_delivery_direct_exchange(self, detect_quorum_queues, caplog):
        self.app.amqp = MagicMock(name='amqp')
        self.app.amqp.router.route.return_value = {
            'queue': Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='direct')
            )
        }

        self.app.send_task('foo', (1, 2), countdown=10)

        self.app.amqp.send_task_message.assert_called_once_with(
            ANY,
            ANY,
            ANY,
            queue=Queue(
                'testcelery',
                routing_key='testcelery',
                exchange=Exchange('testcelery', type='direct')
            )
        )

        assert len(caplog.records) == 1
        record: LogRecord = caplog.records[0]
        assert record.levelname == "WARNING"
        assert record.message == (
            "Direct exchanges are not supported with native delayed delivery.\n"
            "testcelery is a direct exchange but should be a topic exchange or "
            "a fanout exchange in order for native delayed delivery to work properly.\n"
            "If quorum queues are used, this task may block the worker process until the ETA arrives."
        )


class test_defaults:

    def test_strtobool(self):
        for s in ('false', 'no', '0'):
            assert not defaults.strtobool(s)
        for s in ('true', 'yes', '1'):
            assert defaults.strtobool(s)
        with pytest.raises(TypeError):
            defaults.strtobool('unsure')


class test_debugging_utils:

    def test_enable_disable_trace(self):
        try:
            _app.enable_trace()
            assert _state.app_or_default == _state._app_or_default_trace
            _app.disable_trace()
            assert _state.app_or_default == _state._app_or_default
        finally:
            _app.disable_trace()


class test_pyimplementation:

    def test_platform_python_implementation(self):
        with conftest.platform_pyimp(lambda: 'Xython'):
            assert pyimplementation() == 'Xython'

    def test_platform_jython(self):
        with conftest.platform_pyimp():
            with conftest.sys_platform('java 1.6.51'):
                assert 'Jython' in pyimplementation()

    def test_platform_pypy(self):
        with conftest.platform_pyimp():
            with conftest.sys_platform('darwin'):
                with conftest.pypy_version((1, 4, 3)):
                    assert 'PyPy' in pyimplementation()
                with conftest.pypy_version((1, 4, 3, 'a4')):
                    assert 'PyPy' in pyimplementation()

    def test_platform_fallback(self):
        with conftest.platform_pyimp():
            with conftest.sys_platform('darwin'):
                with conftest.pypy_version():
                    assert 'CPython' == pyimplementation()


class test_shared_task:

    def test_registers_to_all_apps(self):
        with self.Celery('xproj', set_as_current=True) as xproj:
            xproj.finalize()

            @shared_task
            def foo():
                return 42

            @shared_task()
            def bar():
                return 84

            assert foo.app is xproj
            assert bar.app is xproj
            assert foo._get_current_object()

            with self.Celery('yproj', set_as_current=True) as yproj:
                assert foo.app is yproj
                assert bar.app is yproj

                @shared_task()
                def baz():
                    return 168

                assert baz.app is yproj
