#!/usr/bin/env python3
"""
The simplest simulation workflow you can run is a large number of forward
simulations to generate synthetics from a velocity model. Therefore the
Forward class represents the BASE workflow. All other workflows will build off
of the scaffolding defined by the Forward class.
"""
import os
import sys
from glob import glob
from time import asctime

from seisflows import logger
from seisflows.tools import msg, unix
from seisflows.tools.config import Dict


class Forward:
    """
    Forward Workflow [Workflow Base]
    --------------------------------
    Defines foundational structure for Workflow module. When used standalone 
    is in charge of running forward solver in parallel and (optionally) 
    calculating data-synthetic misfit and adjoint sources.

    Parameters
    ----------
    :type modules: list of module
    :param modules: instantiated SeisFlows modules which should have been
        generated by the function `seisflows.config.import_seisflows` with a
        parameter file generated by seisflows.configure
    :type generate_data: bool
    :param generate_data: How to address 'data' in the workflow:
        - False: real data needs to be provided by the User in
        `path_data/{source_name}/*` in the same format that the solver will
        produce synthetics (controlled by `solver.format`) OR
        - True: 'data' will be generated as synthetic seismograms using
        a target model provided in `path_model_true`. 
    :type stop_after: str
    :param stop_after: optional name of task in task list (use
        `seisflows print tasks` to get task list for given workflow) to stop
        workflow after, allowing user to prematurely stop a workflow to explore
        intermediate results or debug.
    :type export_traces: bool
    :param export_traces: export all waveforms that are generated by the
        external solver to `path_output`. If False, solver traces stored in
        scratch may be discarded at any time in the workflow
    :type export_residuals: bool
    :param export_residuals: export all residuals (data-synthetic misfit) that
        are generated by the external solver to `path_output`. If False,
        residuals stored in scratch may be discarded at any time in the 
        workflow

    Paths
    -----
    :type workdir: str
    :param workdir: working directory in which to perform a SeisFlows workflow.
        SeisFlows internal directory structure will be created here. Default cwd
    :type path_output: str
    :param path_output: path to directory used for permanent storage on disk.
        Results and exported scratch files are saved here.
    :type path_data: str
    :param path_data: path to any externally stored data required by the solver
    :type path_state_file: str
    :param path_state_file: path to a text file used to track the current
        status of a workflow (i.e., what functions have already been completed),
        used for checkpointing and resuming workflows
    :type path_model_init: str
    :param path_model_init: path to the starting model used to calculate the
        initial misfit. Must match the expected `solver_io` format.
    :type path_model_true: str
    :param path_model_true: path to a target model if `case`=='synthetic' and
        a set of synthetic 'observations' are required for workflow.
    :type path_eval_grad: str
    :param path_eval_grad: scratch path to store files for gradient evaluation,
        including models, kernels, gradient and residuals.
    ***
    """
    def __init__(self, modules=None, generate_data=False, stop_after=None,
                 export_traces=False, export_residuals=False, 
                 custom_tasktimes=None,
                 workdir=os.getcwd(), path_output=None, path_data=None,
                 path_state_file=None, path_model_init=None,
                 path_model_true=None, path_eval_grad=None, **kwargs):
        """
        Set default forward workflow parameters

        :type modules: list
        :param modules: list of sub-modules that will be established as class
            attributes by the setup() function. Should not need to be set by the
            user
        """
        # Keep modules hidden so that seisflows configure doesnt count them
        # as 'parameters'
        self._modules = modules

        self.stop_after = stop_after
        self.generate_data = generate_data
        self.export_traces = export_traces
        self.export_residuals = export_residuals

        self.path = Dict(
            workdir=workdir,
            scratch=os.path.join(workdir, "scratch"),
            eval_grad=path_eval_grad or
                      os.path.join(workdir, "scratch", "eval_grad"),
            output=path_output or os.path.join(workdir, "output"),
            model_init=path_model_init,
            model_true=path_model_true,
            state_file=path_state_file or
                       os.path.join(workdir, "sfstate.txt"),
            data=path_data or os.path.join(workdir, "data"),
        )

        self._required_modules = ["system", "solver"]
        self._optional_modules = ["preprocess"]

        # Read in any existing state file which keeps track of workflow tasks
        self._states = {task.__name__: 0 for task in self.task_list}
        if os.path.exists(self.path.state_file):
            for line in open(self.path.state_file, "r").readlines():
                if line.startswith("#"):
                    continue
                key, val = line.strip().split(":")
                self._states[key] = int(val.strip())

    @property
    def task_list(self):
        """
        USER-DEFINED TASK LIST. This property defines a list of class methods
        that take NO INPUT and have NO RETURN STATEMENTS. This defines your
        linear workflow, i.e., these tasks are to be run in order from start to
        finish to complete a workflow.

        This excludes 'check' (which is run during 'import_seisflows') and
        'setup' which should be run separately

        .. note::
            For workflows that require an iterative approach (e.g. inversion),
            this task list will be looped over, so ensure that any setup and
            teardown tasks (run once per workflow, not once per iteration) are
            not included.

        :rtype: list
        :return: list of methods to call in order during a workflow
        """
        return [self.generate_synthetic_data,
                self.evaluate_initial_misfit
                ]

    def check(self):
        """
        Check that workflow has required modules. Run their respective checks
        """
        # Check that required modules have been instantiated
        for req_mod in self._required_modules:
            assert(self._modules[req_mod]), (
                f"'{req_mod}' is a required module for workflow " 
                f"'{self.__class__.__name__}'"
            )
            # Make sure that the modules are actually instances (not e.g., str)
            assert(hasattr(self._modules[req_mod], "__class__")), \
                f"workflow attribute {req_mod} must be an instance"

            # Run check function of these modules
            self._modules[req_mod].check()

        # Tell the user whether optional modules are instantiated
        for opt_mod in self._optional_modules:
            if self._modules[opt_mod]:
                self._modules[opt_mod].check()
            else:
                logger.warning(f"optional module '{opt_mod}' has not been "
                               f"instantiated, some functionality of the "
                               f"'{self.__class__.__name__}' workflow may be "
                               f"skipped")

        # If we are using the preprocessing module, we must have either
        # 1) real data located in `path.data`, or 2) a target model to generate
        # synthetic data, locaed in `path.model_true`
        if bool(self._modules.preprocess):
            if self.generate_data:
                assert(self.path.model_true is not None and
                       os.path.exists(self.path.model_true)), (
                    f"option `generate_data` requires 'path_model_true' "
                    f"to exist, which points to a target model"
                    )
                assert(self.path.data is not None), \
                    f"`path_data` is required for data-synthetic comparisons"

        if self.stop_after is not None:
            _task_names = [task.__name__ for task in self.task_list]
            assert(self.stop_after in _task_names), \
                f"workflow parameter `stop_after` must match {_task_names}"
            logger.info(f"`workflow.stop_after` == {self.stop_after}")

    def setup(self):
        """
        Assigns modules as attributes of the workflow. I.e., `self.solver` to
        access the solver module (or `workflow.solver` from outside class)

        Makes required path structure for the workflow, runs setup functions
        for all the required modules of this workflow.
        """
        logger.info(f"setup {self.__class__.__name__} workflow")

        # Create the desired directory structure
        for path in self.path.values():
            if path is not None and not os.path.splitext(path)[-1]:
                unix.mkdir(path)

        # Run setup() for each of the required modules
        for req_mod in self._required_modules:
            logger.debug(
                f"running setup for module "
                f"'{req_mod}.{self._modules[req_mod].__class__.__name__}'"
            )
            self._modules[req_mod].setup()

        # Run setup() for each of the optional modules
        for opt_mod in self._optional_modules:
            if self._modules[opt_mod] and opt_mod not in self._required_modules:
                logger.debug(
                    f"running setup for module "
                    f"'{opt_mod}.{self._modules[opt_mod].__class__.__name__}'"
                )
                self._modules[opt_mod].setup()

        # Generate the state file to keep track of task completion
        if not os.path.exists(self.path.state_file):
            with open(self.path.state_file, "w") as f:
                f.write(f"# SeisFlows State File\n")
                f.write(f"# {asctime()}\n")
                f.write(f"#'1: complete', '0: pending', '-1: failed'\n")
                f.write(f"# ========================================\n")

        # Distribute modules to the class namespace. We don't do this at init
        # incase _modules was set as NoneType
        self.solver = self._modules.solver  # NOQA
        self.system = self._modules.system  # NOQA
        self.preprocess = self._modules.preprocess  # NOQA

    def checkpoint(self):
        """
        Saves active SeisFlows working state to disk as a text files such that
        the workflow can be resumed following a crash, pause or termination of
        workflow.
        """
        # Grab State file header values
        with open(self.path.state_file, "r") as f:
            lines = f.readlines()

        with open(self.path.state_file, "w") as f:
            # Rewrite header values
            for line in lines:
                if line.startswith("#"):
                    f.write(line)
            for key, val in self._states.items():
                f.write(f"{key}: {val}\n")

    def run(self):
        """
        Call the Task List in order to 'run' the workflow. Contains logic for
        to keep track of completed tasks and avoids re-running tasks that have
        previously been completed (e.g., if you are restarting your workflow)
        """
        logger.info(msg.mjr(f"RUNNING {self.__class__.__name__.upper()} "
                            f"WORKFLOW"))
        n = 0  # To keep track of number of tasks completed
        for func in self.task_list:
            # Skip over functions which have already been completed
            if (func.__name__ in self._states.keys()) and (
                    self._states[func.__name__] == 1):  # completed
                logger.info(f"'{func.__name__}' has already been run, skipping")
                continue
            # Otherwise attempt to run functions that have failed or are
            # encountered for the first time
            else:
                try:
                    func()
                    n += 1
                    self._states[func.__name__] = 1  # completed
                    self.checkpoint()
                except Exception as e:
                    self._states[func.__name__] = -1  # failed
                    self.checkpoint()
                    raise
            # Allow user to prematurely stop a workflow after a given task
            if self.stop_after and func.__name__ == self.stop_after:
                logger.info(f"stop workflow at `stop_after`: {self.stop_after}")
                break

        self.checkpoint()
        logger.info(f"completed {n} tasks in requested task list successfully")

    def generate_synthetic_data(self, **kwargs):
        """
        For synthetic inversion cases, we can use the workflow machinery to
        generate 'data' by running simulations through a target/true model for 
        each of our `ntask` sources. This only needs to be run once during a 
        workflow.
        """
        if not self.generate_data:
            return

        logger.info(msg.mnr("GENERATING SYNTHETIC DATA W/ TARGET MODEL"))

        # Check the target model that will be used to generate data
        logger.info("checking true/target model parameters:")
        self.solver.check_model_values(path=self.path.model_true)

        self.system.run([self._generate_synthetic_data_single], **kwargs)

    def _generate_synthetic_data_single(self, path_model=None, 
                                        _copy_function=unix.ln, **kwargs):
        """
        Barebones forward simulation to create synthetic data and export and 
        save the synthetics in the correct locations. Hijacks function
        `run_forward_simulations` but uses some different path exports. 

        Exports data to disk in `path_data` and then symlinks to solver 
        directories for each source.

        .. note::

            Must be run by system.run() so that solvers are assigned 
            individual task ids/ working directories.

        :type path_model: str
        :type path_model: path to the model files that will be used to evaluate,
            defaults to `path_model_true`
        :param _copy_function: how to transfer data from `path_data` to scratch
            - unix.ln (default): symlink data to avoid copying large amounts of
                data onto the scratch directory.
            - unix.cp: copy data to avoid burdening filesystem that actual data
                resides on, or to avoid touching the original data on disk. 
        """
        # Set default arguments
        path_model = path_model or self.path.model_true
        save_traces = os.path.join(self.path.data, self.solver.source_name)

        # Run forward simulation with solver
        self.run_forward_simulations(
            path_model=path_model, export_traces=None,  
            save_traces=save_traces, save_forward=False
            )
        
        # Symlink data into solver directories so it can be found by preprocess
        src = os.path.join(save_traces, "*")
        dst = os.path.join(self.solver.cwd, "traces", "obs")

        for src_ in glob(src):
            _copy_function(src_, dst)

    def evaluate_initial_misfit(self, path_model=None, save_residuals=None,
                                _preproc_only=False, **kwargs):
        """
        Evaluate the initial model misfit. This requires setting up 'data'
        before generating synthetics, which is either copied from user-supplied
        directory or running forward simulations with a target model. Forward
        simulations are then run and prepocessing compares data-synthetic misfit

        .. note::

            This is run altogether on system to save on queue time waits,
            because we are potentially running two simulations back to back.

        :type path_model: str
        :param path_model: path to the model files that will be used to evaluate
            initial misfit. If not given, defaults to searching for model
            provided in `path_model_init`.
        :type save_residuals: str
        :param save_residuals: Location to save 'residuals_*.txt files which are
            used to calculate total misfit (f_new), requires a string formatter
            {src} so that the preprocessing module can generate a new file for
            each source. Remainder of string is some combination of the
            iteration, step count etc. Allows inheriting workflows to
            override this path if more specific file naming is required.
        :type _preproc_only: bool
        :param _preproc_only: a debug tool to ONLY run the preprocessing 
            contained in `evaluate_objective_function`, skipping over the 
            forward simulation. You would want to do this, e.g., if your 
            workflow already ran the forward simulation and you just want to 
            re pick windows, or test out different filter bands etc. 
            Recommended this be run in debug mode and that you change `tasktime`
            to reflect that no forward simulation will be run.
        """
        logger.info(msg.mnr("EVALUATING MISFIT FOR INITIAL MODEL"))

        # Forward workflow may not have access to optimization module, so we 
        # only tag residuals files with the source name
        if save_residuals is None:
            save_residuals = os.path.join(self.path.eval_grad, "residuals",
                                          "residuals_{src}.txt")
        else:
            # Require that `save_residuals` has an f-string formatter 'src' that
            # allows each source process to write to its own file
            assert("{src}" in save_residuals), (
                f"Workflow path `save_residuals` requires string formatter "
                "{src} within the string name"
            )

        # Check if we can read in the models to disk prior to submitting jobs
        # this may exit the workflow if we get a read error
        if path_model is None:
            logger.info("evaluating misfit for model in `path_model_init`")
            path_model = self.path.model_init

        logger.info("checking model parameters:")
        self.solver.check_model_values(path=path_model)

        # If no preprocessing module, then all the additional functions for
        # working with `data` are unncessary.
        if self.preprocess:
            run_list = [self.prepare_data_for_solver,
                        self.run_forward_simulations, 
                        self.evaluate_objective_function]
            # Manual overwrite to not run forward simulations
            if _preproc_only:
                logger.warning("user request that NO forward simulation be run")
                run_list = [self.prepare_data_for_solver, 
                            self.evaluate_objective_function]
        else:
            run_list = [self.run_forward_simulations]

        self.system.run(run_list, path_model=path_model,
                        save_residuals=save_residuals,
                        **kwargs
                        )

    def prepare_data_for_solver(self, _src=None, _copy_function=unix.ln,
                                **kwargs):
        """
        Determines how to provide data to each of the solvers. Either by
        symlinking (or copying) data in from a user-provided path, or by
        generating synthetic 'data' by running forward simulations through the
        target model. This usually only needs to be run once per workflow, even
        for inversions

        .. note ::

            Must be run by system.run() so that solvers are assigned individual
            task ids and working directories

        :type _src: str
        :param _src: internal variable used by child classes which inherit
            from Forward, allowing other workflows to change the default path
            that data is searched for. Needs to be a wildcard. 
            By default this function looks at the following wildcard path:
            '{path_data}/{source_name}/*'
        :type _copy_function: function
        :param _copy_function: how to transfer data from `path_data` to scratch
            - unix.ln (default): symlink data to avoid copying large amounts of
                data onto the scratch directory.
            - unix.cp: copy data to avoid burdening filesystem that actual data
                resides on, or to avoid touching the original data on disk.
        """
        # Location to store 'observation' data
        dst = os.path.join(self.solver.cwd, "traces", "obs", "")

        # Check if there is data already in the directory, User may have
        # manually input it here, or we are on iteration > 1 so data has already
        # been prepared, either way, make sure we don't overwrite it
        if glob(os.path.join(dst, "*")):
            logger.warning(f"data already found in "
                           f"{self.solver.source_name}/traces/obs/*, "
                           f"skipping data preparation"
                           )
            return

        logger.info(f"preparing observation data for source "
                    f"{self.solver.source_name}")
        src = _src or os.path.join(self.path.data,
                                   self.solver.source_name, "*")
        logger.debug(f"looking for data in: '{src}'")

        # If no data are found, exit this process, as we cannot continue
        if not glob(src):
            logger.critical(msg.cli(
                f"{self.solver.source_name} found no `obs` data with "
                f"wildcard: '{src}'. Please check `path_data` or manually "
                f"import data and re-submit", border="=",
                header="data import error")
            )
            sys.exit(-1)

        for src_ in glob(src):
            # Symlink or copy data to scratch dir. (symlink by default)
            _copy_function(src_, dst)

    def run_forward_simulations(self, path_model, save_traces=None,
                                export_traces=None, save_forward=None, 
                                **kwargs):
        """
        Performs forward simulation through model saved in `path_model` for a
        single event. Upon successful completion of forward simulation,
        synthetic waveforms are moved to location `save_traces` for processing,
        and/or exported permanently to location on disk `export_traces`.

        .. note::

            if PAR.PREPROCESS == None, will not perform misfit quantification

        .. note::

            Must be run by system.run() so that solvers are assigned individual
            task ids/ working directories.

        :type path_model: str
        :param path_model: path to SPECFEM model files used to run the forwarsd
            simulations. Files will be copied to each individual solver
            directory.
        :type save_traces: str
        :param save_traces: full path location to save synthetic traces after
            successful completion of forward simulations. By default, they are
            stored in 'scratch/solver/<SOURCE_NAME>/traces/syn'. Overriding
            classes may re-direct synthetics by setting this variable
        :type export_traces: str
        :param export_traces: full path location to export (copy) synthetic
            traces after successful completion of forward simulations. Each fwd
            simulation erases the synthetics of the previous forward simulation,
            so exporting to disk is important if the User wants to save
            waveform data. Set parameter `export_traces` True in the parameter
            file to access this option. Overriding classes may re-direct
            synthetics by setting this variable.
        :type save_forward: bool
        :param save_forward: whether to turn on the flag for saving the forward
            arrays which are used for adjoint simulations. Not required if only
            running forward simulations
        """
        logger.info(f"evaluating objective function for source "
                    f"{self.solver.source_name}")
        logger.debug(f"running forward simulation with "
                     f"'{self.solver.__class__.__name__}'")

        # Default value for saving waveforms for processing
        if save_traces is None:
            save_traces = os.path.join(self.solver.cwd, "traces", "syn")

        # Default value for exporting waveforms to disk to save
        if self.export_traces:
            # e.g., output/solver/{source}/syn/*
            export_traces = export_traces or \
                            os.path.join(self.path.output, "solver",
                                         self.solver.source_name, "syn")
        else:
            export_traces = False

        assert(os.path.exists(path_model)), \
            f"Model path for objective function does not exist"

        # We will run the forward simulation with the given input model
        self.solver.import_model(path_model=path_model)

        # Forward workflows do not require saving the large forward arrays
        # because the assumption is that we will not be running adj simulations
        if save_forward is None:
            if self.__class__.__name__ == "Forward":
                save_forward = False
                logger.info("'Forward' workflow, will not save forward array")
            else:
                save_forward = True

        self.solver.forward_simulation(save_traces=save_traces,
                                       export_traces=export_traces, 
                                       save_forward=save_forward
                                       )

    def evaluate_objective_function(self, save_residuals=False, components=None,
                                    **kwargs):
        """
        Uses the preprocess module to evaluate the misfit/objective function
        given synthetics generated during forward simulations

        .. note::

            Must be run by system.run() so that solvers are assigned individual
            task ids/ working directories.

        :type save_residuals: str
        :param save_residuals: if not None, path to write misfit/residuls to
        :type components: list
        :param components: optional list of components to ignore preprocessing
            traces that do not have matching components. The adjoint sources for
            these components will be 0. E.g., ['Z', 'N']. If None, all available
            components will be considered.
        """
        # These are only required for overriding workflows which may hijack
        # this function to provide specific arguments to preprocess module
        iteration = kwargs.get("iteration", 1)
        step_count = kwargs.get("step_count", 0)
        save_adjsrcs = kwargs.get("save_adjsrcs", 
                                  os.path.join(self.solver.cwd, "traces", "adj")
                                  )

        if self.preprocess is None:
            logger.debug("no preprocessing module selected, will not evaluate "
                         "objective function")
            return

        if save_residuals:
            # Check that the calling workflow has properly set the string fmtr.
            assert ("{src}" in save_residuals), (
                "objective function evaluation requires string formatter {} " 
                f"in `save_residuals`: {save_residuals}"
            )
            save_residuals = save_residuals.format(src=self.solver.source_name)

        if self.export_residuals:
            export_residuals = os.path.join(self.path.output, "residuals")
        else:
            export_residuals = False

        logger.debug(f"quantifying misfit with "
                     f"'{self.preprocess.__class__.__name__}'")

        self.preprocess.quantify_misfit(
            source_name=self.solver.source_name, components=components,
            save_adjsrcs=save_adjsrcs, save_residuals=save_residuals,
            export_residuals=export_residuals,
            iteration=iteration, step_count=step_count
        )