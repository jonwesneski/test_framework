import concurrent.futures
import logging
import traceback

from test_framework.discovery import discover_suites
from test_framework.enums import Status, RunMode
from test_framework.logger import LogManager
from test_framework.popo import (
    Result,
    StopTestRunException,
    TestMethod,
    TestMethodResult,
    TestModule,
    TestModuleResult,
    TestSuiteResult,
)


def create_test_suite_instance(suite_paths: list, stop_on_first_failure: bool = False, test_parameters_func=None,
                               log_manager: LogManager = None) -> tuple:
    log_manager_ = log_manager or LogManager('suite_run')
    sequential_modules, parallel_modules, ignored_modules, failed_imports = discover_suites(suite_paths)
    sequential_module_runs = tuple(
        TestModuleRun(x, stop_run=stop_on_first_failure, test_parameters_func=test_parameters_func, log_manager=log_manager_)
        for x in sequential_modules
    )
    parallel_module_runs = tuple(
        TestModuleRun(x, stop_run=stop_on_first_failure, test_parameters_func=test_parameters_func, log_manager=log_manager_)
        for x in parallel_modules
    )
    return (TestSuiteRun(f"\"{' '.join(suite_paths)}\"", sequential_module_runs, parallel_module_runs, log_manager_),
            ignored_modules, failed_imports)


class Run:
    def __init__(self, test_parameters_func=None, logger=None):
        self.test_parameters_func = test_parameters_func or Run._create_default_parameters
        self.logger = logger or logging.getLogger()

    def run_func(self, func, logger=None) -> Result:
        logger_ = logger or self.logger
        result = None
        if func:
            args, kwargs = self.test_parameters_func(logger_)
            result = Result(func.__name__, Status.FAILED)
            try:
                func(*args, **kwargs)
                result.status = Status.PASSED
            except AssertionError as ae:
                result.message = ae
                logger_.error(ae)
            except Exception as e:
                result.message = e
                logger_.error(e)
            result.end()
        return result

    @staticmethod
    def _create_default_parameters(logger) -> tuple:
        return (logger,), {}


class TestSuiteRun:
    def __init__(self, name: str, sequential_modules: tuple, parallel_modules: tuple, log_manager: LogManager = None):
        self.name = name
        self.sequential_modules = sequential_modules
        self.parallel_modules = parallel_modules
        self.suite_result = None
        self.log_manager = log_manager or LogManager()

    def execute(self, parallel: bool) -> TestSuiteResult:
        self.suite_result = TestSuiteResult(self.name)
        self.log_manager.on_suite_start()
        try:
            if parallel:
                for module in self.sequential_modules:
                    self.suite_result.append(module.execute(parallel=False))
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    future_results = {executor.submit(module.execute, parallel): module for module in self.parallel_modules}
                    for future_result in concurrent.futures.as_completed(future_results):
                        try:
                            test_module_result = future_result.result()
                            if test_module_result:
                                self.suite_result.append(test_module_result)
                        except StopTestRunException:
                            raise
                        except Exception as exc:
                            self.log_manager.test_run_logger.error(exc)
            else:
                for module in self.sequential_modules + self.parallel_modules:
                    self.suite_result.append(module.execute(parallel=False))
        except StopTestRunException as stre:
            self.log_manager.test_run_logger.error(stre)
        except Exception:
            self.log_manager.test_run_logger.error(traceback.format_exc())
        self.suite_result.end()
        self.log_manager.on_suite_stop(self.suite_result)
        return self.suite_result


class TestModuleRun(Run):
    def __init__(self, test_module: TestModule, stop_run: bool, test_parameters_func=None, log_manager: LogManager = None):
        super().__init__(test_parameters_func)
        self.test_module = test_module
        self.stop_run = stop_run
        self.log_manager = log_manager

    def execute(self, parallel: bool) -> TestModuleResult:
        setup = self.setup()
        if setup is None or setup.status == Status.PASSED:
            test_module_result = self.run(parallel and self.test_module.module.__run_mode__==RunMode.PARALLEL_TEST)
        else:
            test_results = [TestMethodResult(test.name, status=Status.SKIPPED) for test in self.test_module.tests]
            test_module_result = TestModuleResult(self.test_module.name, test_results=test_results, status=Status.SKIPPED)
        test_module_result.setup = setup
        test_module_result.teardown = self.teardown()
        test_module_result.end()
        self.log_manager.on_module_done(test_module_result)
        return test_module_result

    def setup(self) -> Result:
        logger = None if not self.test_module.setup else self.log_manager.get_setup_logger(self.test_module.name)
        result = self.run_func(self.test_module.setup, logger)
        self.log_manager.on_setup_module_done(self.test_module.name, result)
        return result

    def run(self, parallel: bool) -> TestModuleResult:
        test_module_result = TestModuleResult(self.test_module.name)
        if parallel:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                future_results = {
                    executor.submit(TestMethodRun(self.test_module.name, test, self.stop_run, self.test_parameters_func, self.log_manager).execute): test
                    for test in self.test_module.tests
                }
                for future_result in concurrent.futures.as_completed(future_results):
                    try:
                        test_result = future_result.result()
                        if test_result:
                            test_module_result.append(test_result)
                            if self.stop_run and test_result.status == Status.FAILED:
                                raise StopTestRunException(test_result.message)
                    except StopTestRunException:
                        raise
                    except Exception:
                        self.logger.error(traceback.format_exc())
        else:
            for test in self.test_module.tests:
                test_module_result.append(
                    TestMethodRun(self.test_module.name, test, self.stop_run, self.test_parameters_func, self.log_manager).execute()
                )
                if self.stop_run and test_module_result.test_results[-1].status == Status.FAILED:
                    raise StopTestRunException(test_module_result.test_results[-1].message)
        return test_module_result

    def teardown(self) -> Result:
        logger = None if not self.test_module.teardown else self.log_manager.get_teardown_logger(self.test_module.name)
        result = self.run_func(self.test_module.teardown, logger)
        self.log_manager.on_teardown_module_done(self.test_module.name, result)
        return result


class TestMethodRun(Run):
    def __init__(self, module_name: str, test_method: TestMethod, stop_run: bool, test_parameters_func, log_manager: LogManager):
        super().__init__(test_parameters_func, None)
        self.module_name = module_name
        self.test_method = test_method
        self.log_manager = log_manager
        self.stop_run = stop_run

    def execute(self) -> TestMethodResult:
        setup = self.setup()
        if setup is None or setup.status == Status.PASSED:
            result = self.run()
        else:
            result = TestMethodResult(self.test_method.name, setup, status=Status.SKIPPED)
            parameterized_results = []
            if hasattr(self.test_method.func, 'parameterized_list'):
                for i in range(len(self.test_method.func.parameterized_list)):
                    parameterized_results.append(Result(f'{self.test_method.name}[{i}]', status=Status.SKIPPED))
            result.parameterized_results = parameterized_results
        result.setup = setup
        result.teardown = self.teardown()
        result.end()
        self.log_manager.on_test_done(self.module_name, result)
        return result

    def setup(self) -> Result:
        result = self.run_func(self.test_method.setup_func, self.log_manager.get_setup_test_logger(self.module_name, self.test_method.name))
        if result:
            self.log_manager.on_setup_test_done(self.module_name, self.test_method.name, result)
        return result

    def run(self) -> TestMethodResult:
        result = TestMethodResult(self.test_method.name)
        if hasattr(self.test_method.func, 'parameterized_list'):
            if self.test_method.func.is_parallel:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    def execute(i, parameters_list):
                        def parameters_func(logger):
                            args, kwargs = self.test_parameters_func(logger)
                            return args+parameters_list[i], kwargs
                        parameter_run = Run(parameters_func, self.log_manager.get_test_logger(self.module_name, f'{self.test_method.name}[{i}]'))
                        parameter_result = parameter_run.run_func(self.test_method.func)
                        parameter_result.name = f'{self.test_method.name}[{i}]'
                        self.log_manager.on_parameterized_test_done(self.module_name, parameter_result)
                        return parameter_result
                    future_results = {
                        executor.submit(execute, i, self.test_method.func.parameterized_list): i
                        for i in self.test_method.func.range
                    }
                    for future_result in concurrent.futures.as_completed(future_results):
                        try:
                            parameter_result = future_result.result()
                            if parameter_result:
                                result.append(parameter_result)
                                if self.stop_run and parameter_result.status == Status.FAILED:
                                    raise StopTestRunException(parameter_result.message)
                        except StopTestRunException:
                            raise
                        except Exception:
                            self.logger.error(traceback.format_exc())
            else:
                for i in self.test_method.func.range:
                    def parameters_func(logger):
                        args, kwargs = self.test_parameters_func(logger)
                        return args+self.test_method.func.parameterized_list[i], kwargs
                    parameter_run = Run(parameters_func, self.log_manager.get_test_logger(self.module_name, f'{self.test_method.name}[{i}]'))
                    parameter_result = parameter_run.run_func(self.test_method.func)
                    parameter_result.name = f'{self.test_method.name}[{i}]'
                    self.log_manager.on_parameterized_test_done(self.module_name, parameter_result)
                    result.append(parameter_result)
        else:
            logger = self.log_manager.get_test_logger(self.module_name, self.test_method.name)
            try:
                args, kwargs = self.test_parameters_func(logger)
                self.test_method.func(*args, **kwargs)
                result.status = Status.PASSED
            except AssertionError as ae:
                result.message = ae
                result.status = Status.FAILED
                logger.error(ae)
            except Exception as e:
                result.message = e
                result.status = Status.FAILED
                logger.error(e)
        return result

    def teardown(self) -> Result:
        result = self.run_func(self.test_method.teardown_func, self.log_manager.get_teardown_test_logger(self.module_name, self.test_method.name))
        if result:
            self.log_manager.on_teardown_test_done(self.module_name, self.test_method.name, result)
        return result
