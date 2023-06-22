#!/usr/bin/env python3
"""
Ambient Noise Adjoint Tomography Forward Solver based on the workflow proposed
by Wang et al. where synthetic Greens functinos (SGF) are generated by
simulating point forces.

.. note:: Kernel Naming

    The kernel naming convention used for naming in this workflow follows from
    reference 1 and follows from the ambient noise community. The convention is
    two letter names (e.g., AB) where the first letter (e.g., A) represents the
    input force direction, and the second letter (e.g., B) represents the
    component on which the wavefield is recorded. So ZZ represents an
    upward (+Z) force recorded on Z components. In ambient noise, the
    common EGFs are ZZ, TT and RR.

.. note:: References

    1. "Three‐dimensional sensitivity kernels for multicomponent empirical
        Green's functions from ambient noise: Methodology and application to
        Adjoint tomography."
        Journal of Geophysical Research: Solid Earth 124.6 (2019): 5794-5810.
"""
import os
import numpy as np
from glob import glob
from seisflows import logger
from seisflows.tools import unix, msg
from seisflows.workflow.inversion import Inversion


class NoiseInversion(Inversion):
    """
    Noise Inversion Workflow
    ------------------------
    Run forward and adjoint solvers to produce Synthetic Greens Functions (SGF)
    based on unidirectional forces which are meant to represent virtual
    sources of uniform noise distributions. SGFs are compared to Empirical
    Greens Functions (EGF) for iterative model updates.

    .. note:: simulation requirements per source station

        - 'ZZ' kernel requires 1 forward (Z) and 1 adjoint (Z) simulation
        - 'TT or RR' kernel requires 2 forward (N + E) and 2 adjoint (N_? + E_?)
           simulations (where ? = R or T)
        - 'TT,RR' kernels can share their 2 forward simulations (N + E) but
          require 4 separate adjoint simulations (N_T + E_T + N_R + E_R)
        - 'ZZ,TT or ZZ,RR' requires 3 forward (Z + N + E) and 3 adjoint
        - 'ZZ,TT,RR' requires 3 forward (Z + N + E) and 5 adjoint
           (Z + N_T + E_T + N_R + E_R)

    Parameters
    ----------
    :type kernels: str
    :param kernels: comma-separated list of kernels to generate w.r.t available
        EGF data. Corresponding data must be available. Available options are:

        - 'ZZ': vertical component force recorded on vertical component.
          Represents Rayleigh wave energy
        - 'TT': transverse copmonent force recorded on transverse component.
          Represents Love wave energy
        - 'RR': radial component force recorded on radial component.
          Represents Rayleigh wave energy

        Example inputs would be 'ZZ' or 'ZZ,TT' or 'ZZ,TT,RR'. Case insensitive

    Paths
    -----

    ***
    """
    __doc__ = Inversion.__doc__ + __doc__

    def __init__(self, kernels="ZZ", preprocess="noise", **kwargs):
        """
        Initialization of the Noise Inversion Workflow module
        """
        super().__init__(**kwargs)

        self.kernels = kernels.upper()

        self._preprocess = preprocess

        # Internal variables control behavior of spawend jobs
        self._force = None  # direction of input force for fwd/adj simulation
        self._cmpnt = None  # component of output synthetics/adjsrcs used

    def check(self):
        """
        Additional checks for the Noise Inversion Workflow
        """
        super().check()

        assert(self._preprocess == "noise"), \
            f"Noise Inversion workflow require the `noise` preprocessing class"

        acceptable_kernels = {"ZZ", "TT", "RR"}
        assert(set(self.kernels.split(",")).issubset(acceptable_kernels)), \
            f"`kernels` must be a subset of {acceptable_kernels}"

        assert(self.data_case == "data"), \
            f"Noise Inversion workflow must have `data_case` == 'data'"

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
        task_list = []

        # Determine which kernels we will generate during the workflow
        if "ZZ" in self.kernels:
            task_list.append(self.generate_zz_kernels)
        # These components can be run together because they use the same sims
        if "TT" in self.kernels or "RR" in self.kernels:
            task_list.append(self.generate_tt_rr_kernels)

        # Standard inversion tasks
        task_list.extend([
            self.postprocess_event_kernels,
            self.evaluate_gradient_from_kernels,
            self.initialize_line_search,
            self.perform_line_search,
            self.finalize_iteration
        ])

        return task_list

    def trace_path(self, tag, comp=None):
        """
        Convenience path function that returns the full path for storing
        intermediate waveform files for a given component. 
        These generally adhere to how the `solver` module names directories. 
        
        Required because this workflow will do a lot of pre-rotation waveform 
        storage, so we use this function as the once-and-for-all definition for 
        the paths

        .. note ::

            Must be run by system.run() so that solvers are assigned individual
            task ids and working directories

        :type tag: str
        :param tag: sub directory tag, e.g., 'syn' to store synthetic waveforms 
            and 'adj' to store adjoint sources.
        :type comp: str
        :param comp: optional component used to tag the sub directory
        :rtype: str
        :return: full path to solver scratch traces directory to save waveforms
        """
        if comp is not None:
            tag = f"{tag}_{comp}".lower()
        return os.path.join(self.solver.cwd, "traces", tag)

    def prepare_data_for_solver(self, **kwargs):
        """
        Overrides workflow.forward.prepare_data_for_solver() by changing
        the location of expected observed data, and removing any data
        previously stored within the `solver/traces/obs/` directory.

        Looks for data in the following locations:
            ZZ kernel: `path_data`/{source_name}/ZZ/*
            RR kernel: `path_data`/{source_name}/RR/*
            TT kernel: `path_data`/{source_name}/TT/*

        This will be run within the `evaluate_initial_misfit` function

        .. note ::

            Must be run by system.run() so that solvers are assigned individual
            task ids and working directories
        """
        # Define where the obs data is stored
        dst = self.trace_path("obs")

        # Remove any existing data that might have been placed here previously
        # to avoid incorporating it into preprocessing
        unix.rm(glob(os.path.join(dst, "*")))

        # Internal source attribute defines what data are required, we can have
        # both RR and TT EGFs
        wc = ""  # wildcard to search for data
        if "RR" in self.kernels:
            wc += "R"
        if "TT":
            wc += "T"
        # Generating a wildcard string that will be used to copy in data
        dir_ = {"Z": "ZZ",
                "N": f"[{wc}][{wc}]",  # [RT][RT] -> both RR and TT
                "E": f"[{wc}][{wc}]"}[self._force]

        src = os.path.join(self.path.data, self.solver.source_name, dir_, "*")

        # Use Forward workflow machinery to copy in data
        super().prepare_data_for_solver(_src=src)

    def run_forward_simulations(self, path_model, **kwargs):
        """
        Overrides the `forward.run_forward_simulation` to do some additional
        file manipulations and output file redirects to prepare for noise
        inversion, prior to running the forward simulation.

        .. note::

            Internal parameter `_force` needs to be set by the calling
            functions prior to running forward simulations.

        .. note::

            Must be run by system.run() so that solvers are assigned individual
            task ids/ working directories.
        """
        assert(self._force is not None), (
            f"`run_forward_simulation` requires that the internal attribute " 
            f"`_force` is set prior to running forward simulations"
        )

        # Edit the force vector based on the internal value for chosen kernel
        kernel_vals, save_traces = None, None
        if self._force == "Z":
            kernel_vals = ["0.d0", "0.d0", "1.d0"]  # [E, N, Z]
        else:
            if self._force == "N":
                kernel_vals = ["0.d0", "1.d0", "0.d0"]  # [E, N, Z]
            elif self._force == "E":
                kernel_vals = ["1.d0", "0.d0", "0.d0"]  # [E, N, Z]
            # e.g., solver/{source_name}/traces/syn_e
            save_traces = self.trace_path(tag="syn", comp=self._force)

        # Set FORCESOLUTION (3D/3D_GLOBE) to ensure correct force for kernel
        self.solver.set_parameters(keys=["component dir vect source E",
                                         "component dir vect source N",
                                         "component dir vect source Z_UP"],
                                   vals=kernel_vals, file="DATA/FORCESOLUTION",
                                   delim=":")

        super().run_forward_simulations(path_model, save_traces=save_traces,
                                        **kwargs)

        # TODO >redirect output `export_traces` seismograms to honor kernel name

    def evaluate_objective_function(self, save_residuals=False, components=None,
                                    **kwargs):
        """
        Modifications to original Inverse workflow function to allow quantifying
        misfit for RR and TT kernels which require seismogram rotations prior 
        to running preprocessing.

        This will be run within the `evaluate_initial_misfit` function

        .. note::

            Must be run by system.run() so that solvers are assigned individual
            task ids/ working directories.
        """
        # Z component force behaves like a normal inversion
        if self._force == "Z":
            super().evaluate_objective_function(components=["Z"])
        # E and N component force need to wait for one another
        else:
            # Check if we have generated all the necessary synthetics before
            # running preprocessing
            n_traces = glob(os.path.join(self.trace_path("syn", "n"), "*"))
            e_traces = glob(os.path.join(self.trace_path("syn", "e"), "*"))
            if not n_traces or not e_traces:
                logger.info("not all required synthetics present for RR/TT "
                            "kernels, skipping preprocessing")

                # Create a dummy residuals file so that the original function
                # doesn't complain when it tries to read it. Sort of hacky.
                # !!! Assuming filename based on function
                # !!! `inversion.evaluate_misfit_function`
                np.savetxt(
                        os.path.join(self.path.eval_grad, "residuals.txt"), [0]
                        )
                return

            # This will generate RR and TT synthetics in `traces/syn` with
            # synthetics generated using `traces/syn_e` and `traces/syn_n`
            logger.info("rotating N and E synthetics to RR and TT components")
            self.preprocess.rotate_ne_traces_to_rt(
                source_name=self.solver.source_name,
                syn_path=self.trace_path(tag="syn", comp="{}"),
                data_wildcard=self.solver.data_wildcard(comp="{}"),
                kernels=self.kernels
            )
            # Run preprocessing with rotated synthetics for N and E only
            super().evaluate_objective_function(save_residuals=save_residuals,
                                                components=["T", "R"]
                                                )
            # Re-rotate T and R adjoint sources to N and E components for 
            # adjoint simulations. Only rotate what is required for adj sim.
            for choice in ["T", "R"]:
                if choice in self.kernels:
                    self.preprocess.rotate_rt_adjsrcs_to_ne(
                        source_name=self.solver.source_name,
                        adj_path=self.trace_path(tag="adj", comp=None),
                        choice=choice,
                        )

    def generate_zz_kernels(self):
        """
        Generate Synthetic Greens Functions (SGF) for the ZZ component by
        running forward simulations for each master station using a Z component
        force, and then running an adjoint simulation to generate kernels.
        """
        # This will be referenced in `run_forward_simulations`
        self._force = "Z"
        self._cmpnt = "Z"

        # Run the forward solver to generate SGFs and adjoint sources
        super().evaluate_initial_misfit()

        # Run the adjoint solver to generate kernels for ZZ sensitive structure
        super().run_adjoint_simulations()

    def generate_tt_rr_kernels(self):
        """
        Generate Synthetic Greens Functions (SGF) for the TT and/or RR
        component(s) following Wang et al. (2019).

        .. note::

            This is significantly more complicated than the ZZ case because we
            need to rotate back and forth between the N and E simulations, and
            the R and T EGFs.

        Workflow steps are as follows:

        1. Run E component forward simulation, save traces & forward arrays
        2. Run N component forward simulations, save traces & forward arrays
        3. Rotate N and E component SGF to R and T components based on
           source-receiver azimuth values
        4. Calculate RR and TT adjoint sources (u_rr, u_tt) w.r.t EGF data

        5a. Rotate u_tt to N and E (u_ee, u_en, u_ne, u_nn)
        6a. Run ET adjoint simulation (injecting u_ee, u_en) for K_ET
        7a. Run NT adjoint simulation (injecting u_ne, u_nn) for K_NT
        8a. Sum T kernels, K_ET + K_NT = K_TT

        5a. Rotate u_rr to N and E (u_ee, u_en, u_ne, u_nn)
        6b. Run ER adjoint simulation (injecting u_ee, u_en) for K_ER
        7b. Run NR adjoint simulation (injecting u_ne, u_nn) for K_NR
        8b. Sum R kernels, K_ER + K_NR = K_RR

        9. Sum kernels K = K_RR + K_TT
        """
        logger.info(msg.mnr("EVALUATING RR/TT MISFIT FOR INITIAL MODEL"))

        # Run the forward solver to generate ET SGFs and adjoint sources
        # Note, this must be run BEFORE 'NN' to get preprocessing to work
        self._force = "E"
        logger.info(f"running misfit evaluation for component '{self._force}'")
        super().evaluate_initial_misfit()

        # Run the forward solver to generate SGFs and adjoint sources
        self._force = "N"
        logger.info(f"running misfit evaluation for component '{self._force}'")
        super().evaluate_initial_misfit()

        # Run adjoint simulations for each kernel RR and TT (if requested) by 
        # running two adjoint simulations (E and N) per kernel. 
        for cmpnt in ["T", "R"]:  
            # Skip over if User did not request 
            if cmpnt not in self.kernels:  # e.g., if 'R' in 'RR,TT'
                continue

            # Set internal kernel variable which will let all spawned jobs
            # know which set of adjoint sources are required for their sim
            logger.info(f"running generating kernel for component: {cmpnt}")
            self._cmpnt = cmpnt  # T or R

            # We require two adjoint simulations per kernel to recover gradient
            for force in ["E", "N"]:
                self._force = force
                logger.info(f"running adjoint simulation for "
                            f"'{self._force}{self._cmpnt}'")
                self.run_adjoint_simulations()

            # Unset internal variables just incase
            self._cmpnt = None
            self._force = None

    def run_adjoint_simulations(self, **kwargs):
        """
        Overwrite the Workflow.Migration function to perform adjoint source
        rotation prior to adjoint simulation. Only required for RR and TT kernel
        """
        subdir = f"{self._force}{self._cmpnt}"  # one of: ZZ, NT, ET, NR, ER

        # Save and export kernels 
        save_kernels = os.path.join(self.path.eval_grad, "kernels",
                                    self.solver.source_name, subdir)
        export_kernels = os.path.join(self.path.output, "kernels",
                                      self.solver.source_name, subdir)

        super().run_adjoint_simulations(save_kernels=save_kernels,
                                        export_kernels=export_kernels,
                                        **kwargs)

    def _run_adjoint_simulation_single(self, save_kernels=None, 
                                       export_kernels=None, **kwargs):
        """
        Overwrites Migration workflow function to: 1) create necessary empty 
        adjoint sources, 2) prepend a data retrieval operation to the RR or TT 
        adjoint simulation so that the correct adjoint sources are discoverable 
        during the solver sim.

        .. note::

            Must be run by system.run() so that solvers are assigned
            individual task ids/working directories.
        """
        if self._force == "Z":
            self._generate_empty_adjsrcs(components=["E", "N"])
            
            super()._run_adjoint_simulation_single(save_kernels, export_kernels)
        elif self._force in ["E", "N"]:
            self._generate_empty_adjsrcs(components=["Z"])

            # Symlink the correct set of adjoint sources to the 'adj' directory
            # `adj_dir` is something like 'adj_nt'
            adj_dir = f"adj_{self._force.lower()}{self._cmpnt.lower()}" 

            srcs = glob(os.path.join(self.solver.cwd, "traces", adj_dir, "*"))
            dst = os.path.join(self.solver.cwd, "traces", "adj")
            for src in srcs:
                unix.ln(src, dst)

            super()._run_adjoint_simulation_single(save_kernels, export_kernels)

            # Get rid of symlinks to make room for next simulation
            for fid in glob(os.path.join(dst, "*.adj")):
                if os.path.islink(fid):
                    unix.rm(fid)

    def _generate_empty_adjsrcs(self, components):
        """
        Generate empty (zero amplitude) adjoint sources for every station and
        given `component`. Uses the Solver and Preprocess modules to get after
        file naming and trace characteristics.

        .. note::

            Must be run by system.run() so that solvers are assigned
            individual task ids/working directories.

        :type components: list of str
        :param components: components to generate empty adjoint sources for.
            e.g., ['E', 'N'] will generate E and N component adjoint sources.
            Note that any files matching the output adjoint source file name
            will be removed so ensure that there is no actual adjoint source
            data in this file.
        """
        # Grab a dummy synthetic trace to use for time series structure
        st = self.preprocess.read(fid=self.solver.data_filenames("syn")[0],
                                  data_format=self.solver.syn_data_format)
        st[0].data *= 0  # zero amplitude adjoint source

        # Get list of synthetic traces which require a corresponding adj source
        # and rename them so that they follow the expected SPECFEM format
        adj_fids = [
                self.preprocess.rename_as_adjoint_source(os.path.basename(f))
                for f in self.solver.data_filenames("syn")
                ]

        # Replace the channel component with the user-requested components
        # !!! Making assumptions about the filenaming structure here
        channel = adj_fids[0].split(".")[2]  # e.g., MXT
        chnfmt = channel[:2] + "{}"  # e.g., MX{}

        for fid in adj_fids:
            for comp in components:
                adjpath = os.path.join(self.trace_path("adj"), 
                                       fid.replace(channel, chnfmt.format(comp))
                                       )
                self.preprocess.write(st=st, fid=adjpath)

    def postprocess_event_kernels(self):
        """
        Overwrite the Migration function to combine multiple event kernels
        (ZZ, ET + NT, ER + NR) prior to smoothing kernels. 

        Assumes the sub-directory structure of kernels for path `eval_grad`
        """
        # We need to combine the N? and E? kernels for EACH source
        if "RR" in self.kernels:
            
            input_paths = [os.path.join(self.path.eval_grad, "ER"),
                           os.path.join(self.path.eval_grad, "NR")
                           ]
            output_path = 

        if "TT" in self.kernels:
            pass

        super().postprocess_event_kernels()


    def _evaluate_line_search_misfit(self):
        """
        Used in line search for calculating misfit values to compare against
        starting model. Here we overwrite the base function to allow rotating 
        synthetics N+E -> R+T.

        .. note::
            Each call of this function will save residuals but these will be 
            ignored and the final residual file will only be created once all 
            forward simulations are run
        """
        if "ZZ" in self.kernels:
            self._force = "Z"
            super()._evaluate_line_search_misfit()
        elif "RR" in self.kernels or "TT" in self.kernels:
            self._force = "N"
            super()._evaluate_line_search_misfit()

            self._force = "E"
            super()._evaluate_line_search_misfit()


