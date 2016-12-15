'''
optimizer.py -- Optimization routines for PRMS parameters and data.
'''
from __future__ import print_function
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import datetime as dt
import os, sys, json, re
import multiprocessing as mp

from copy import copy
from copy import deepcopy
from numpy import log10

from .data import Data
from .parameters import Parameters
from .simulation import Simulation, SimulationSeries
from .util import load_statvar, nash_sutcliffe, percent_bias, rmse

OPJ = os.path.join


class Optimizer:
    '''
    Container for a PRMS parameter optimization routine consisting of the
    four stages as described in Hay, et al, 2006
    (ftp://brrftp.cr.usgs.gov/pub/mows/software/luca_s/jawraHay.pdf).

    Example:

    >>> from prms_python import Data, Optimizer, Parameters
    >>> params = Parameters('path/to/parameters')
    >>> data = Data('path/to/data')
    >>> control = 'path/to/control'
    >>> work_directory = 'path/to/create/simulations'
    >>> optr = Optimizer(params, data, control, work_directory, \
               title='the title', description='desc')
    >>> srad_hru = 2490
    >>> optr.srad('path/to/reference_data/measured_srad.csv', srad_hru)

    '''
        
    #dic for min/max of parameter allowable ranges, add more when needed
    param_ranges = {'dday_intcp': (-60.0, 10.0), 'dday_slope': (0.2, 0.9),\
                    'jh_coef': (0.005, 0.06), 'pt_alpha': (1.0, 2.0), \
                    'potet_coef_hru_mo': (1.0, 1.6), 'tmax_index': (-10.0, 110.0)\
                    } #changed potet max 

    def __init__(self, parameters, data, control_file, working_dir,
                 title, description=None):

        if isinstance(parameters, Parameters):
            self.parameters = parameters
        else:
            raise TypeError('parameters must be instance of Parameters')

        if isinstance(data, Data):
            self.data = data

        else:
            raise TypeError('data must be instance of Data')

        input_dir = '{}'.format(os.sep).join(control_file.split(os.sep)[:-1])
        if not os.path.isfile(OPJ(input_dir, 'statvar.dat')):
            print('You have no statvar.dat file in your current model directory')
            print('Running PRMS on original data in {} for later comparison'\
                  .format(input_dir))
            sim = Simulation(input_dir)
            sim.run()

        if not os.path.isdir(working_dir):
            os.mkdir(working_dir)
        
        self.input_dir = input_dir
        self.control_file = control_file
        self.working_dir = working_dir
        self.title = title
        self.description = description
        self.srad_outputs = []
        self.measured_srad = None
        self.srad_hru = None 
        self.pet_outputs = []
        self.measured_pet = None
        self.pet_hru = None
        self.measured_arb = None # for arbitrary output methods
        self.arb_statvar = None
        self.arb_outputs = []

    def monte_carlo(self, reference_path, param_names, statvar_name, stage='custom',\
                   n_sims=10, method='uniform', noise_factor=0.1, nproc=None):
        '''
        Optimize the monthly dday_intcp and dday_slope parameters 
        (two key parameters in the ddsolrad module in PRMS) by one of
        multiple methods (in development): Monte Carlo default method

        Args:
            reference_srad_path (str): path to measured solar radiation data
            station_nhru (int): hru index in PRMS that is geographically 
                near the measured solar radiation location. Must
                have swrad 'nhru' listed as a statvar output in your 
                control file. If 'basin' then statvar variable 'basin_swrad_1'
        Kwargs:
            method (str): XXX not yet implemented- 
            n_sims (int): number of simulations to conduct 
                parameter optimization/uncertaitnty analysis.
        '''
        # assign the optimization object a copy of measured srad for plots
        self.measured_arb = pd.Series.from_csv(
            reference_path, parse_dates=True
        )
        # for retrieving statistical variable output at basin or hru scale 
        self.arb_statvar = statvar_name

        start_time = dt.datetime.now()
        start_time = start_time.replace(second=0, microsecond=0)

        ## shifting all monthly values by random amount from uniform distribution
        ## resampling degree day slope and intercept simoultaneously

        params = []
        for name in param_names: # list of lists of resampled params
            tmp = []
            for idx in range(n_sims):
                tmp.append(resample_param(self.parameters, name, how=method,\
                                          noise_factor=noise_factor))
            params.append(list(tmp))
            
        # sim dirs named after first resampled param name and mean value
        series = SimulationSeries(
            Simulation.from_data(
                self.data, _mod_params(self.parameters,\
                                       [params[n][i] for n in range(len(params))],\
                                       param_names),
                self.control_file,
                OPJ(
                    self.working_dir,
                    '{0}:{1:.6f}'.format(param_names[0], np.mean(params[0][i]))
                )
            )
            for i in range(n_sims)
        )

        # run all scenarios
        outputs = list(series.run(nproc=nproc).outputs_iter())        
        self.arb_outputs.extend(outputs) 

        end_time = dt.datetime.now()
        end_time = end_time.replace(second=0, microsecond=0)
        
        if not nproc: nproc = mp.cpu_count() // 2        

        meta = { 'params_adjusted' : param_names,
                 '{}_statvar_name'.format(stage) : self.arb_statvar,
                 'optimization_title' : self.title,
                 'optimization_description' : self.description,
                 'start_time' : str(start_time),
                 'end_time' : str(end_time),
                 'measured_{}'.format(stage) : reference_path,
                 'resample': method,
                 'sim_dirs' : [],
                 'stage': '{}'.format(stage),
                 'original_params' : self.parameters.base_file,
                 'nproc': nproc,
                 'n_sims' : n_sims
               }

        for output in outputs:
            meta['sim_dirs'].append(output['simulation_dir'])
        
        json_outfile = OPJ(self.working_dir, _create_metafile_name(\
                          self.working_dir, self.title, stage))
      
        with open(json_outfile, 'w') as outf:  
            json.dump(meta, outf, sort_keys = True, indent = 4,\
                      ensure_ascii = False)

        print('{0}\nOutput information sent to {1}\n'.format('-' * 80, json_outfile))

    def srad(self, reference_srad_path, station_nhru, n_sims=10, method='uniform',\
             noise_factor=0.1, srad_mod='ddsolrad', nproc=None):
        '''
        Optimize the monthly dday_intcp and dday_slope parameters 
        (two key parameters in the ddsolrad module in PRMS) by one of
        multiple methods (in development): Monte Carlo default method

        Args:
            reference_srad_path (str): path to measured solar radiation data
            station_nhru (int): hru index in PRMS that is geographically 
                near the measured solar radiation location. Must
                have swrad 'nhru' listed as a statvar output in your 
                control file. If 'basin' then statvar variable 'basin_swrad_1'
        Kwargs:
            method (str): XXX not yet implemented- 
            n_sims (int): number of simulations to conduct 
                parameter optimization/uncertaitnty analysis.
        '''
        # assign the optimization object a copy of measured srad for plots
        self.measured_srad = pd.Series.from_csv(
            reference_srad_path, parse_dates=True
        )
        # for retrieving statistical variable output at basin or hru scale 
        if station_nhru == 'basin': 
            self.srad_hru = 'basin_swrad_1' 
        else: # get variable at a specific hru
            self.srad_hru = 'swrad_{}'.format(station_nhru)

        srad_start_time = dt.datetime.now()
        srad_start_time = srad_start_time.replace(second=0, microsecond=0)

        ## shifting all monthly values by random amount from uniform distribution
        ## resampling degree day slope and intercept simoultaneously
        if srad_mod == 'ddsolrad':
            param_names = ['dday_intcp', 'dday_slope'] 

        params = []
        for name in param_names: # list of lists of resampled params
            tmp = []
            for idx in range(n_sims):
                tmp.append(resample_param(self.parameters, name, how=method,\
                                          noise_factor=noise_factor))
            params.append(list(tmp))
            
        self.data.write(OPJ(self.working_dir, 'data'))

        # sim dirs named after first resampled param name and mean value
        series = SimulationSeries(
            Simulation.from_data(
                self.data, _mod_params(self.parameters,\
                                       [params[n][i] for n in range(len(params))],\
                                       param_names),
                self.control_file,
                OPJ(
                    self.working_dir,
                    '{0}:{1:.6f}'.format(param_names[0], np.mean(params[0][i]))
                )
            )
            for i in range(n_sims)
        )

        # run all scenarios
        outputs = list(series.run(nproc=nproc).outputs_iter())        
        self.srad_outputs.extend(outputs) 

        srad_end_time = dt.datetime.now()
        srad_end_time = srad_end_time.replace(second=0, microsecond=0)

        if not nproc: nproc = mp.cpu_count() // 2        

        srad_meta = {'stage' : 'swrad',
                     'params_adjusted' : param_names,
                     'swrad_statvar_name' : self.srad_hru,
                     'optimization_title' : self.title,
                     'optimization_description' : self.description,
                     'start_time' : str(srad_start_time),
                     'end_time' : str(srad_end_time),
                     'measured_swrad' : reference_srad_path,
                     'resample': method,
                     'sim_dirs' : [],
                     'original_params' : self.parameters.base_file,
                     'nproc': nproc,
                     'n_sims' : n_sims
                    }

        for output in outputs:
            srad_meta['sim_dirs'].append(output['simulation_dir'])
        
        json_outfile = OPJ(self.working_dir, _create_metafile_name(\
                          self.working_dir, self.title, 'swrad'))
      
        with open(json_outfile, 'w') as outf:  
            json.dump(srad_meta, outf, sort_keys = True, indent = 4,\
                      ensure_ascii = False)

        print('{0}\nOutput information sent to {1}\n'.format('-' * 80, json_outfile))

    def pet(self, reference_pet_path, station_nhru, n_sims=10,\
             pet_mod='potet_pt', method='uniform', noise_factor=0.1, nproc=None):
        '''
        Optimize the monthly coefficients (depending on module) parameters 
        by one of multiple methods (in development): Monte Carlo default method

        Args:
            reference_pet_path (str): path to measured pet data
            station_nhru (int or str): hru index in PRMS that is geographically 
                near the measured/estimated pet location. If value
                is 'basin' then you wish to compare to area-weighted 
                basin-wide simulated pet (`basin_potet_1` in PRMS).
        Kwargs:
            method (str): XXX not yet implemented- (default-Monte Carlo)
            n_sims (int): number of simulations to conduct 
                parameter optimization/uncertaitnty analysis.
        '''
        # assign the optimization object a copy of measured pet 
        self.measured_pet = pd.Series.from_csv(
            reference_pet_path, parse_dates=True
        )

        if station_nhru == 'basin': 
            self.pet_hru = 'basin_potet_1' 
        else: # get variable at a specific hru
            self.pet_hru = 'potet_{}'.format(station_hru)

        pet_start_time = dt.datetime.now()
        pet_start_time = pet_start_time.replace(second=0, microsecond=0)

        if pet_mod == 'potet_pt':
            param_names = ['potet_coef_hru_mo'] 
        elif pet_mod == 'potet_jh':
            param_names = ['jh_coef']
        # list of lists of resampled params
        params = []
        for name in param_names: 
            tmp = []
            for idx in range(n_sims):
                tmp.append(resample_param(self.parameters, name, how=method,\
                                          noise_factor=noise_factor))
            params.append(list(tmp))
            
        # sim dirs named after first resampled param name and mean value
        series = SimulationSeries(
            Simulation.from_data(
                self.data, _mod_params(self.parameters,\
                                       [params[n][i] for n in range(len(params))],\
                                       param_names),
                self.control_file,
                OPJ(
                    self.working_dir,
                    '{0}:{1:.6f}'.format(param_names[0], np.mean(params[0][i]))
                )
            )
            for i in range(n_sims)
        )
        
        # run all scenarios
        outputs = list(series.run(nproc=nproc).outputs_iter())        
        self.pet_outputs.extend(outputs) 

        pet_end_time = dt.datetime.now()
        pet_end_time = pet_end_time.replace(second=0, microsecond=0)

        if not nproc: nproc = mp.cpu_count() // 2        

        pet_meta = {'stage' : 'pet',
                     'params_adjusted' : param_names,
                     'pet_statvar_name' : self.pet_hru,
                     'optimization_title' : self.title,
                     'optimization_description' : self.description,
                     'start_time' : str(pet_start_time),
                     'end_time' : str(pet_end_time),
                     'measured_pet' : reference_pet_path,
                     'resample': method,
                     'sim_dirs' : [],
                     'original_params' : self.parameters.base_file,
                     'nproc': nproc,
                     'n_sims' : n_sims
                    }

        for output in outputs:
            pet_meta['sim_dirs'].append(output['simulation_dir'])
        
        json_outfile = OPJ(self.working_dir, _create_metafile_name(\
                          self.working_dir, self.title, 'pet'))
      
        with open(json_outfile, 'w') as outf:  
            json.dump(pet_meta, outf, sort_keys = True, indent = 4,\
                      ensure_ascii = False)

        print('{0}\nOutput information sent to {1}\n'.format('-' * 80, json_outfile))

    def plot_optimization(self, stage, freq='daily', method='time_series',\
                          plot_vars='both', plot_1to1=True, return_fig=False,\
                          n_plots=4):
        """
        Basic plotting of current optimization results with limited options. 
        Plots measured, original simluated, and optimization simulated variabes
        either swrad, pet, or streamflow (TODO) depending on stage at the 
        corresponding HRU or basin-wide scale either as time series (all three) 
        or scatter (measured versus simulated). Not recommended for plotting 
        results when n_sims, instead use options from an OptimizationResult 
        object, or employ a user-defined method using the result data if necessary.

        Kwargs:
            stage (str): stage of optimization to plot (swrad,pet,flow)
            freq (str): frequency of time series plots, value can be 'daily'
                or 'monthly' for solar radiation 
            method (str): 'time_series' for time series sub plot of each
                simulation alongside measured radiation. Another choice is 
                'correlation' which plots each measured daily solar radiation
                value versus the corresponding simulated variable as subplots
                one for each simulation in the optimization. With coefficients
                of determiniationi i.e. square of pearson correlation coef.  
            plot_vars (str): what to plot alongside simulated srad: 
                'meas': plot simulated along with measured swrad
                'orig': plot simulated along with the original simulated swrad
                'both': plot simulated, with original simulation and measured
            plot_1to1 (bool): if True plot one to one line on correlation 
                scatter plot, otherwise exclude.
            return_fig (bool): flag whether to return matplotlib figure 
        Returns: 
            f (matplotlib.figure.Figure): If kwarg return_fig=True, then return
                copy of the figure that is generated to the user. 
        """
        #use optimization stage to set plot parameters and get appropriate data
        if (stage=='swrad'):
            if not self.srad_outputs:
                raise ValueError('You have not run any srad optimizations')
            var_name = self.srad_hru # name in statvar file 
            #indices that measured and simulated share (intersection)
            X = self.measured_srad
            idx = X.index.intersection(self.srad_outputs[0]['statvar']\
                              ['{}'.format(var_name)].index)
            X = X[idx]
            orig = load_statvar(OPJ(self.input_dir, 'statvar.dat'))['{}'\
                                .format(var_name)][idx]
            meas = self.measured_srad[idx]
            sims = [out['statvar']['{}'.format(var_name)][idx] for \
                    out in self.srad_outputs] # list of pd.Series
            simdirs = [out['simulation_dir'].split(os.sep)[-1].replace('_', ' ')\
                       for out in self.srad_outputs] # names from simulation dirs
            var_name = 'shortwave radiation'
            n = len(self.srad_outputs) # number of simulations to plot
        elif (stage=='pet'):
            if not self.pet_outputs:
                raise ValueError('You have not run any pet optimizations')
            var_name = self.pet_hru
            X = self.measured_pet
            idx = X.index.intersection(self.pet_outputs[0]['statvar']\
                              ['{}'.format(var_name)].index)
            X = X[idx]
            orig = load_statvar(OPJ(self.input_dir, 'statvar.dat'))['{}'\
                                .format(var_name)][idx]
            meas = self.measured_pet[idx]
            sims = [out['statvar']['{}'.format(var_name)][idx] for \
                    out in self.pet_outputs]
            simdirs = [out['simulation_dir'].split(os.sep)[-1].replace('_', ' ')\
                       for out in self.pet_outputs]
            var_name = 'potential ET'
            n = len(self.pet_outputs) # number of simulations to plot
        elif (stage=='custom'):
            if not self.arb_outputs:
                raise ValueError('You have not run any custom optimizations')
            var_name = self.arb_statvar
            X = self.measured_arb
            idx = X.index.intersection(self.arb_outputs[0]['statvar']\
                               ['{}'.format(var_name)].index)
            X = X[idx]
            orig = load_statvar(OPJ(self.input_dir, 'statvar.dat'))['{}'\
                                .format(var_name)][idx]
            meas = self.measured_arb[idx]
            sims = [out['statvar']['{}'.format(var_name)][idx] for \
                    out in self.arb_outputs]
            simdirs = [out['simulation_dir'].split(os.sep)[-1].replace('_', ' ')\
                       for out in self.arb_outputs]
            var_name = '{}'.format(self.arb_statvar)
            n = len(self.arb_outputs) # number of simulations to plot
        else:
            raise ValueError('{} is not a valid optimization stage.'.format(stage))
        # user defined number of subplots from first n_plots results 
        if (n > n_plots): n = n_plots  
        # styles for each plot
        ms = 4 # markersize for all points
        orig_sty = dict(linestyle='none',markersize=ms,\
                           markerfacecolor='none', marker='s',\
                           markeredgecolor='royalblue', color='royalblue') 
        meas_sty = dict(linestyle='none',markersize=ms+1,\
                           markerfacecolor='none', marker='1',\
                           markeredgecolor='k', color='k') 
        sim_sty = dict(linestyle='none',markersize=ms,\
                           markerfacecolor='none', marker='o',\
                           markeredgecolor='r', color='r') 
        ## number of subplots and rows (two plots per row) 
        nrow = n//2 # round down if odd n
        ncol = 2
        odd_n = False
        if n/2. - nrow == 0.5: 
            nrow+=1 # odd number need extra row
            odd_n = True
        ########
        ## Start plots depnding on key word arguments
        ########
        if freq == 'daily' and method == 'time_series':
            fig, ax = plt.subplots(n, sharex=True, sharey=True,\
                                   figsize=(12,n*3.5))
            axs = ax.ravel()
            for i,sim in enumerate(sims[:n]):
                if plot_vars in ('meas', 'both'):
                    axs[i].plot(meas, label='Measured', **meas_sty)
                if plot_vars in ('orig', 'both'):
                    axs[i].plot(orig, label='Original sim.', **orig_sty)
                axs[i].plot(sim, **sim_sty)
                axs[i].set_ylabel('sim: {}'.format(simdirs[i]), fontsize=10)
                if i == 0: axs[i].legend(markerscale=5, loc='best')
            fig.subplots_adjust(hspace=0)
            fig.autofmt_xdate()
        #monthly means
        elif freq == 'monthly' and method == 'time_series':
            # compute monthly means
            meas = meas.groupby(meas.index.month).mean()
            orig = orig.groupby(orig.index.month).mean()
            # change line styles for monthly plots to lines not points
            for d in (orig_sty, meas_sty, sim_sty):
                d['linestyle'] = '-'
                d['marker'] = None

            fig, ax = plt.subplots(nrows=nrow, ncols=ncol, figsize=(12,n*3.5))
            axs = ax.ravel()
            for i,sim in enumerate(sims[:n]):
                if plot_vars in ('meas', 'both'):
                    axs[i].plot(meas, label='Measured', **meas_sty)
                if plot_vars in ('orig', 'both'):
                    axs[i].plot(orig, label='Original sim.', **orig_sty)
                sim = sim.groupby(sim.index.month).mean()
                axs[i].plot(sim, **sim_sty)
                axs[i].set_ylabel('sim: {}\nmean {}'.format(simdirs[i], stage),\
                                  fontsize=10)
                axs[i].set_xlim(0.5,12.5)
                if i == 0: axs[i].legend(markerscale=5, loc='best')
            if odd_n: # empty subplot if odd number of simulations 
                fig.delaxes(axs[n])
            fig.text(0.5, 0.1, 'month') 
        #x-y scatter
        elif method == 'correlation':
            ## figure
            fig, ax = plt.subplots(nrows=nrow, ncols=ncol, figsize=(12,n*3))
            axs = ax.ravel()
            ## subplot dimensions
            meas_min = min(X) 
            meas_max = max(X)
            
            for i, sim in enumerate(sims[:n]):
                Y = sim
                sim_max = max(Y)
                sim_min = min(Y)
                m = max(meas_max,sim_max)
                if plot_1to1:
                    axs[i].plot([0, m], [0, m], 'k--', lw=2) ## one to one line
                axs[i].set_xlim(meas_min,meas_max)
                axs[i].set_ylim(sim_min, sim_max)
                axs[i].plot(X, Y, **sim_sty)
                axs[i].set_ylabel('sim: {}'.format(simdirs[i]))
                axs[i].set_xlabel('Measured {}'.format(var_name))
                axs[i].text(0.05, 0.95,r'$R^2 = {0:.2f}$'.format(\
                            X.corr(Y)**2), fontsize=16,\
                            ha='left', va='center', transform=axs[i].transAxes)  
            if odd_n: # empty subplot if odd number of simulations 
                fig.delaxes(axs[n])      

        if return_fig:
            return fig

def _create_metafile_name(out_dir, opt_title, stage):
    """
    Search through output directory where simulations are conducted
    look for all metadata simulation json files and find out if the
    current simulation is a replicate. Then use that information to 
    build the correct file name for the output json file. The series
    are typically run in parallel that is why this step has to be 
    done after running multiple simulations from an optimization stage.

    Args:
        out_dir (str): path to directory with model results, i.e. 
            location where simulation series outputs and optimization
            json files are located, aka Optimizer.working_dir
        opt_title (str): optimization instance title for file search
    stage (str): stage of optimization, e.g. 'swrad', 'pet' 

    Returns:
    name (str): file name for the current optimization simulation series
        metadata json file. E.g 'dry_creek_swrad_opt.json', or if
        this is the second time you have run an optimization titled
        'dry_creek' the next json file will be returned as 
        'dry_creek_swrad_opt1.json' and so on with integer increments     
    """
    meta_re = re.compile(r'^{}_{}_opt(\d*)\.json'.format(opt_title, stage))
    reps = []
    for f in os.listdir(out_dir): 
        if meta_re.match(f):
            nrep = meta_re.match(f).group(1)
            if nrep == '': 
                reps.append(0)
            else:
                reps.append(nrep)

    if not reps: 
        name = '{}_{}_opt.json'.format(opt_title, stage)
    else:
        # this is the nth optimization done under the same title
        n = max(map(int, reps)) + 1
        name = '{}_{}_opt{}.json'.format(opt_title, stage, n)
    return name

def resample_param(params, param_name, how='uniform', noise_factor=0.1):
    """
    Resample PRMS parameter by shifting all values by a constant that is 
    taken from a uniform distribution, where the range of the uniform 
    values is equal to the difference between the min(max) of the parameter
    set and the min(max) of the allowable range from PRMS. For parameters 
    that have array length <= 366 add noise to each parameter element by 
    adding a RV from a normal distribution with mean 0, sigma = param 
    allowable range / 10.  
    
    Args:
        params (parameters.Parameters): parameter object 
        param_name (str): name of PRMS parameter to resample
    Kwargs: 
        how (str): distribution to resample parameters from in the case 
            that each parameter element can be resampled (len <=366)
            Currently works for uniform and normal distributions. 
        noise_factor (float): factor to multiply parameter range by, 
            use the result as the standard deviation for the normal rand.
            variable used to add element wise noise. i.e. higher 
            noise facter will result in higher noise added to each param
            element. Must be > 0.
    Returns:
        ret (numpy.ndarry): ndarray of param after uniform random mean 
            shift or element-wise noise addition (normal r.v.) 
    """
    p_min, p_max = Optimizer.param_ranges.get(param_name,(-1,-1))
    
    # create dictionary of parameter basic info (not values)
    param_dic = {param['name']: param for param in params.base_params}
    if not param_dic.get(param_name):
        raise KeyError('{} is not a valid parameter'.format(param_name))
        
    if p_min == p_max == -1:
        raise ValueError("""{} has not been added to the dictionary of
        parameters to resample, add it's allowable min and max value
        to the param_ranges dictionary in the resample function in
        Optimizer.py""".format(param_name))
        
    dim_case = None
    nhru = params.dimensions['nhru']
    ndims = param_dic.get(param_name)['ndims']
    dimnames = param_dic.get(param_name)['dimnames']
    length = param_dic.get(param_name)['length']
    param = params[param_name]
    
    # could expand list and check parameter name also e.g. cascade_flg
    # is a parameter that should not be changed 
    dims_to_not_change = set(['ncascade','ncascdgw','nreach',\
                             'nsegment']) 
    if (len(set.intersection(dims_to_not_change, set(dimnames))) > 0):
        raise ValueError("""{} should not be resampled as
                          it relates to the location of cascade flow
                          parameters.""".format(param_name))
        
    # use param info to get dimension info- e.g. if multidimensional
    if (ndims == 1 and length <= 366):
        dim_case = 'resample_each_value' # covers anything above one outside of nhru
    elif (ndims == 1 and length > 366):
        dim_case = 'resample_all_values_once' # covers nssr, ngw, etc. 
    elif (ndims == 2 and dimnames[1] == 'nmonths' and \
          nhru == params.dimensions[dimnames[0]]):
        dim_case = 'nhru_nmonths'   
    elif not dim_case:
        raise ValueError('The {} parameter should not be resampled'.\
                         format(param_name))        
#     #testing purposes    
#     print('name: ', param_name)
#     print('max_val: ', p_max)
#     print('min_val: ', p_min)
#     print('ndims: ', ndims)
#     print('dimnames: ', dimnames)
#     print('length: ', length)
#     print('resample_method: ', dim_case)

    low_bnd = p_min - np.min(param) # lowest param value minus allowable min
    up_bnd = p_max - np.max(param)
    s = (p_max - p_min) * noise_factor # variance noise, default: range*(1/10)  
    #do resampling differently based on param dimensions 
    if dim_case == 'resample_all_values_once': 
        #uniform RV for shifting all values once
        shifted_param = np.random.uniform(low=low_bnd, high=up_bnd) + param
        ret = shifted_param
    elif dim_case == 'resample_each_value':
        ret = copy(param)
        if how == 'uniform':
            for i, el in enumerate(param):
                low_bnd = p_min - np.min(el) # a,b for uniform RV to add 
                up_bnd = p_max - np.max(el)
                ret[i] = el + np.random.uniform(low=low_bnd, high=up_bnd)
        elif how == 'normal':
            for i, el in enumerate(param):
                while True:
                    low_bnd = p_min - np.min(el) 
                    up_bnd = p_max - np.max(el)
                    tmp = el + np.random.normal(0, s)
                    if np.max(tmp) <= p_max and np.min(tmp) >= p_min:
                        ret[i] = tmp
                        break
        
    elif dim_case == 'nhru_nmonths':
        ret = copy(param)
        rvs = [np.random.uniform(low=low_bnd, high=up_bnd) for i in range(12)]
        for month in range(12):
            ret[month] += rvs[month]

    return ret

def _mod_params(parameters, params, param_names):
    # deepcopy was crashing, raising:
    # TypeError: cannot serialize '_io.TextIOWrapper' object
    ret = copy(parameters)
    for idx, param in enumerate(params):
        ret[param_names[idx]] = param
    return ret


class OptimizationResult:
    
    def __init__(self, working_dir, stage='all'):
        self.working_dir = working_dir 
        self.stage = stage
        self.metadata_json_paths = self.get_optr_jsons(working_dir, stage)     
        self.meas_swrad = self.get_measured('swrad')
        self.meas_pet = self.get_measured('pet')
        self.swrad_statvar_name = self.get_statvar_name('swrad')
        self.pet_statvar_name = self.get_statvar_name('pet')

    def get_optr_jsons(self, work_dir, stage):
        """
        Retrieve locations of optimization output jsons which contain 
        important metadata needed to understand optimization results.
        Create dictionary of each optimization stage as keys, and lists
        of corresponding json file paths for each stage as values. 

        Arguments:
            work_dir (str): path to directory with model results, i.e. 
                location where simulation series outputs and optimization
                json files are located, aka Optimizer.working_dir
            stage (str): the stage ('swrad', 'pet', 'flow', etc.) of 
                the optimization in which to gather the jsons, if
                stage is 'all' then each stage will be gathered.
        Returns:
            ret (dict): dictionary of stage (keys) and lists of 
                json file paths for that stage (values).  
        """

        ret = {}
        if stage != 'all':
            optr_metafile_re = re.compile(r'^.*_{}_opt(\d*)\.json'.format(stage))
            ret[stage] = [OPJ(work_dir, f) for f in\
                              os.listdir(work_dir) if\
                              optr_metafile_re.match(f) ]
        else: 
            stages = ['swrad', 'pet', 'flow', 'custom']
            for s in stages:
                optr_metafile_re = re.compile(r'^.*_{}_opt(\d*)\.json'.format(s))
                ret[s] =  [OPJ(work_dir, f) for f in\
                               os.listdir(work_dir) if\
                               optr_metafile_re.match(f) ]
        return ret

    def get_sim_dirs(self, stage):
        jsons = self.metadata_json_paths[stage]
        json_files = []
        sim_dirs = []
        for inf in jsons:
            with open(inf) as json_file:
                json_files.append(json.load(json_file))
        for json_file in json_files:
            sim_dirs.extend(json_file['sim_dirs'])
        # list of all simulation directory paths for stage 
        return sim_dirs

    def get_measured(self, stage):
        # only need to open one json file to get this information
        if not self.metadata_json_paths.get(stage):
            return # no optimization json files exist for given stage
        first_json = self.metadata_json_paths[stage][0]
        with open(first_json) as json_file:
            json_data = json.load(json_file)
        measured_series = pd.Series.from_csv(json_data.get('measured_{}'.\
                                            format(stage)), parse_dates=True)
        return measured_series 

    def get_statvar_name(self, stage):
        # only need to open one json file to get this information
        if not self.metadata_json_paths.get(stage):
            return # no optimization json files exist for given stage
        first_json = self.metadata_json_paths[stage][0]
        with open(first_json) as json_file:
            json_data = json.load(json_file)
        var_name = json_data.get('{}_statvar_name'.format(stage))

        return var_name

    def result_table(self, stage, freq='daily', top_n=5, latex=False):
        ##TODO: add stats for freq options monthly, annual (means or sum)

        sim_dirs = self.get_sim_dirs(stage)
        if top_n >= len(sim_dirs): top_n = len(sim_dirs) 
        sim_names = [path.split(os.sep)[-1] for path in sim_dirs] 
        meas_var = self.get_measured(stage)
        statvar_name = self.get_statvar_name(stage)
        result_df = pd.DataFrame(columns=\
                            ['NSE','RMSE','PBIAS','COEF_DET','ABS(PBIAS)'])
        for i, sim in enumerate(sim_dirs):
            sim_out = load_statvar(OPJ(sim, 'outputs', 'statvar.dat'))\
                                               ['{}'.format(statvar_name)]
            idx = meas_var.index.intersection(sim_out.index)
            meas_var = copy(meas_var[idx])
            sim_out = sim_out[idx]    
            if freq == 'daily':
                result_df.loc[sim_names[i]] = [nash_sutcliffe(meas_var, sim_out),\
                              rmse(meas_var, sim_out),\
                              percent_bias(meas_var,sim_out),\
                              meas_var.corr(sim_out)**2,\
                              np.abs(percent_bias(meas_var, sim_out)) ]    
            elif freq == 'monthly':
                meas_mo = meas_var.groupby(meas_var.index.month).mean()
                sim_out = sim_out.groupby(sim_out.index.month).mean()
                result_df.loc[sim_names[i]] = [nash_sutcliffe(meas_mo, sim_out),\
                              rmse(meas_mo, sim_out),\
                              percent_bias(meas_mo, sim_out),\
                              meas_mo.corr(sim_out)**2,\
                              np.abs(percent_bias(meas_mo, sim_out)) ]    
   
        sorted_result = result_df.sort_values(by=['NSE','RMSE','ABS(PBIAS)',\
                               'COEF_DET'], ascending=[False,True,True,False])
        sorted_result.columns.name = '{} parameters'.format(stage)
        sorted_result = sorted_result[['NSE','RMSE','PBIAS','COEF_DET']] 

        if latex: return sorted_result[:top_n].to_latex(escape=False)
        else: return  sorted_result[:top_n]

    def get_top_ranked_sims(self, sorted_df):
        ## use result table to make dic with best param and statvar paths 
        # index of table is the simulation directory names
        ret = {
              'dir_name' : [],
              'param_path' : [],
              'statvar_path' : []
              }

        for i,el in enumerate(sorted_df.index):
            ret['dir_name'].append(el)
            ret['param_path'].append(OPJ(self.working_dir,el,'inputs','parameters'))
            ret['statvar_path'].append(OPJ(self.working_dir,el,'outputs','statvar.dat'))
        
        return ret

        
class SradOptimizationResult(OptimizationResult):

    def __init__(self, working_dir, stage='swrad' ):
        OptimizationResult.__init__(self, working_dir, stage)
        self.swrad_sim_dirs = self.get_sim_dirs(stage)
        self.meas_swrad = self.get_measured(stage)
        self.swrad_statvar_name = self.get_statvar_name(stage)
        
        
class PetOptimizationResult(OptimizationResult):

    def __init__(self, working_dir, stage='pet'):
        OptimizationResult.__init__(self, working_dir, stage)
        self.pet_sim_dirs = self.get_sim_dirs(stage)
        self.meas_pet = self.get_measured(stage)
        self.pet_statvar_name = self.get_statvar_name(stage)
 
