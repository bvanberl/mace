import sys
import time
import copy
import pickle
import numpy as np
import pandas as pd
import normalizedDistance
import torch
import gurobipy as grb

from modelConversion import *
from pysmt.shortcuts import *
from pysmt.typing import *
from pprint import pprint

from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier

from network_linear_approximation import LinearizedNetwork
from mip_solver import MIPNetwork
from applyMIPConstraints import *

from debug import ipsh

from random import seed
RANDOM_SEED = 1122334455
seed(RANDOM_SEED) # set the random seed so that the random permutations can be reproduced again
np.random.seed(RANDOM_SEED)

# DEBUG_FLAG = True
DEBUG_FLAG = False


def getTorchFromSklearn(dataset_obj, sklearn_model, input_dim, preprocessing=None, no_final_relu=False):
  model_width = sklearn_model.hidden_layer_sizes[0]

  if sklearn_model.hidden_layer_sizes == model_width:
    n_hidden_layers = 1
  else:
    n_hidden_layers = len(sklearn_model.hidden_layer_sizes)

  torch_model = torch.nn.ModuleList()

  if preprocessing is not None:
    # add a layer in the beginning that implements the preprocessing
    # so that the preprocessing constraints are later added to the MIP model
    torch_model.append(torch.nn.Linear(input_dim, input_dim))

  torch_model.append(torch.nn.Linear(input_dim, model_width))
  torch_model.append(torch.nn.ReLU())

  for i in range(n_hidden_layers):

    if i==n_hidden_layers-1:
      torch_model.append(torch.nn.Linear(model_width, 1))
      if not no_final_relu:
        torch_model.append(torch.nn.ReLU())
      continue

    torch_model.append(torch.nn.Linear(model_width, model_width))
    torch_model.append(torch.nn.ReLU())


  if preprocessing is not None:
    assert preprocessing=='normalize', "Currently only range normalization is supported for preprocessing."
    torch_model[0].weight = torch.nn.Parameter(torch.zeros(torch_model[0].weight.shape, dtype=torch.float64),
                                               requires_grad=False)
    torch_model[0].bias = torch.nn.Parameter(torch.zeros(torch_model[0].bias.shape, dtype=torch.float64),
                                             requires_grad=False)
    input_atrr_names = dataset_obj.getInputAttributeNames()
    for i in range(input_dim):
      attr_obj = dataset_obj.attributes_kurz[input_atrr_names[i]]
      lower_bound = attr_obj.lower_bound
      upper_bound = dataset_obj.attributes_kurz[input_atrr_names[i]].upper_bound
      if 'cat' in attr_obj.attr_type or 'ord' in attr_obj.attr_type or 'binary' in attr_obj.attr_type:
        torch_model[0].weight[i][i] = 1.
      else:
        torch_model[0].weight[i][i] = 1 / float(upper_bound - lower_bound)
        torch_model[0].bias[i] = - float(lower_bound) / (float(upper_bound - lower_bound))

  n_prep = 0 if preprocessing is None else 1

  for i in range(n_hidden_layers + 1):
    torch_model[2*i + n_prep].weight = torch.nn.Parameter(torch.tensor(sklearn_model.coefs_[i].astype('float64'),
                                                                       dtype=torch.float64).t(), requires_grad=False)
  for i in range(n_hidden_layers + 1):
    torch_model[2*i + n_prep].bias = torch.nn.Parameter(torch.tensor(sklearn_model.intercepts_[i].astype('float64'),
                                                                     dtype=torch.float64), requires_grad=False)

  return torch_model

def setupMIPModelWithInputVars(dataset_obj):

  # Initial model
  mip_model = grb.Model()
  mip_model.setParam('OutputFlag', False)
  mip_model.setParam('Threads', 1)

  # Initial params
  model_vars = {
    'counterfactual': {},
    'interventional': {},
    'output': {'y': {'var': mip_model.addVar(obj=0, vtype=grb.GRB.BINARY, name='y')}}
  }

  # Populate model_vars['counterfactual'] using the
  # parameters saved during training
  for attr_name_kurz in dataset_obj.getInputAttributeNames('kurz'):
    attr_obj = dataset_obj.attributes_kurz[attr_name_kurz]
    lower_bound = attr_obj.lower_bound
    upper_bound = attr_obj.upper_bound
    if attr_name_kurz not in dataset_obj.getInputAttributeNames('kurz'):
      continue  # do not overwrite the output
    if attr_obj.attr_type == 'numeric-real':
      model_vars['counterfactual'][attr_name_kurz] = {
        'var': mip_model.addVar(lb=float(lower_bound), ub=float(upper_bound), obj=0,
                                vtype=grb.GRB.CONTINUOUS, name=attr_name_kurz),
        'lower_bound': Real(float(lower_bound)),
        'upper_bound': Real(float(upper_bound))
      }
    elif attr_obj.attr_type == 'numeric-int':  # refer to loadData.VALID_ATTRIBUTE_TYPES
      model_vars['counterfactual'][attr_name_kurz] = {
        'var': mip_model.addVar(lb=lower_bound, ub=upper_bound, obj=0,
                                vtype=grb.GRB.INTEGER, name=attr_name_kurz),
        'lower_bound': Real(float(lower_bound)),
        'upper_bound': Real(float(upper_bound))
      }
    elif attr_obj.attr_type == 'binary' or 'cat' in attr_obj.attr_type or 'ord' in attr_obj.attr_type:
      model_vars['counterfactual'][attr_name_kurz] = {
        'var': mip_model.addVar(lb=lower_bound, ub=upper_bound, obj=0,
                                vtype=grb.GRB.BINARY, name=attr_name_kurz),
        'lower_bound': Real(float(lower_bound)),
        'upper_bound': Real(float(upper_bound))
      }
    else:
      raise Exception(f"Variable type {attr_obj.attr_type} not defined.")

  # IMPORTANT: Do not uncomment any of the following if not sure about numeric behavior
  # mip_model.setParam('FeasibilityTol', 1e-9)
  # mip_model.setParam('OptimalityTol', 1e-9)
  # mip_model.setParam('IntFeassTol', 1e-9)

  mip_model.update()

  return mip_model, model_vars

def findCFE4MLP(model_trained, dataset_obj, factual_sample, norm_type, norm_lower, norm_upper, epsilon, preprocessing, diverse_cfs=None):
  assert isinstance(model_trained, MLPClassifier), "Only MLP model supports the linear relaxation."
  input_dim = len(dataset_obj.getInputAttributeNames('kurz'))

  # First, translate sklearn model to PyTorch model
  torch_model = getTorchFromSklearn(dataset_obj, model_trained, input_dim, preprocessing, no_final_relu=True)

  # Now create a linearized network
  layers = [module for module in torch_model]
  mip_net = MIPNetwork(layers)

  # Get input domains
  domains = np.zeros((input_dim, 2), dtype=np.float64)
  for i, attr_name_kurz in enumerate(dataset_obj.getInputAttributeNames('kurz')):
    attr_obj = dataset_obj.attributes_kurz[attr_name_kurz]
    domains[i][0] = attr_obj.lower_bound
    domains[i][1] = attr_obj.upper_bound
  domains = torch.from_numpy(domains)

  # Setup MIP model and check bounds feasibility w.r.t. distance formula
  feasible = mip_net.setup_model(domains, factual_sample, dataset_obj, norm_type, norm_lower, norm_upper, epsilon=epsilon,
                                 sym_bounds=False, dist_as_constr=not('obj' in norm_type), bounds='opt', diverse_cfs=diverse_cfs)
  if not feasible:
    return False, None

  # Solve the MIP
  solved, sol, _ = mip_net.solve(domains, factual_sample)
  # print("opt dist: ", mip_net.model.getVarByName("normalized_distance").x)
  return solved, sol


def findCFE4Others(approach, model_trained, dataset_obj, factual_sample, norm_type, norm_lower=0, norm_upper=0, mip_model=None, epsilon=None):

  # print(mip_model)
  # print("lowe: ", norm_lower, " upper: ", norm_upper)

  if mip_model is not None and 'EXP' in approach: # We are in an iteration of the EXP search
    # Update distance constraints
    mip_model.remove(mip_model.getConstrByName('dist_less_than'))
    mip_model.remove(mip_model.getConstrByName('dist_greater_than'))
    dist_var = mip_model.getVarByName('normalized_distance')
    mip_model.addConstr(dist_var <= norm_upper, name='dist_less_than')
    mip_model.addConstr(dist_var >= norm_lower, name='dist_greater_than')
    mip_model.update()
  elif mip_model is not None:
    raise Exception("MIP model must be none.")
  else:
    # setup the model
    mip_model, model_vars = setupMIPModelWithInputVars(dataset_obj)
    applyPlausibilityConstrs(mip_model, dataset_obj)
    mip_model.update()
    applyDistanceConstrs(mip_model, dataset_obj, factual_sample, norm_type, norm_lower, norm_upper)
    mip_model.update()
    applyTrainedModelConstrs(mip_model, model_vars, model_trained)
    mip_model.update()
    mip_model.addConstr(model_vars['output']['y']['var'] == 1 - int(factual_sample['y']))
    if 'OBJ' in approach:  # set distance as objective for MIP_OBJ
      mip_model.setObjective(mip_model.getVarByName('normalized_distance'), grb.GRB.MINIMIZE)
      mip_model.setParam('OptimalityTol', epsilon)
    mip_model.update()

  if 'two_norm' in norm_type:
    mip_model.setParam('NonConvex', 2)
    mip_model.update()

  mip_model.optimize()

  if 'OBJ' in approach:
    # If we use the OBJECTIVE approach, the MIP must be optimally solved
    assert mip_model.status is grb.GRB.OPTIMAL, f"Model status is not optimal but: {mip_model.status}"
  elif mip_model.status is grb.GRB.INFEASIBLE: # Check feasibility for EXPONENTIAL approach
    return False, None, mip_model

  # Get the input that gives the optimal solution.
  counterfactual_sample = {}
  for feature_name in dataset_obj.getInputAttributeNames('kurz'):
    var = mip_model.getVarByName(feature_name)
    counterfactual_sample[feature_name] = var.x
  counterfactual_sample['y'] = mip_model.getVarByName('y').x

  return True, counterfactual_sample, mip_model

def findClosestCounterfactualSample(model_trained, dataset_obj, factual_sample, norm_type, approach_string, epsilon, log_file, preprocessing=None, k_cfes=None):

  def getCenterNormThresholdInRange(lower_bound, upper_bound):
    return (lower_bound + upper_bound) / 2

  def assertPrediction(dict_sample, model_trained, dataset_obj):
    vectorized_sample = []
    for attr_name_kurz in dataset_obj.getInputAttributeNames('kurz'):
      if preprocessing == 'normalize':
        attr_obj = dataset_obj.attributes_kurz[attr_name_kurz]
        lower_bound = attr_obj.lower_bound
        upper_bound = attr_obj.upper_bound
        if not('cat' in attr_obj.attr_type or 'ord' in attr_obj.attr_type or 'binary' in attr_obj.attr_type):
          vectorized_sample.append((dict_sample[attr_name_kurz]-lower_bound)/(upper_bound-lower_bound))
        else:
          vectorized_sample.append(dict_sample[attr_name_kurz])
      else:
        vectorized_sample.append(dict_sample[attr_name_kurz])

    sklearn_prediction = int(model_trained.predict([vectorized_sample])[0])
    pysmt_prediction = int(dict_sample['y'])
    factual_prediction = int(factual_sample['y'])

    # IMPORTANT: sometimes, MACE does such a good job, that the counterfactual
    #            ends up super close to (if not on) the decision boundary; here
    #            the label is underfined which causes inconsistency errors
    #            between pysmt and sklearn. We skip the assert at such points.
    class_predict_proba = model_trained.predict_proba([vectorized_sample])[0]
    # print(class_predict_proba)
    if np.abs(class_predict_proba[0] - class_predict_proba[1]) < 1e-8:
      return

    if isinstance(model_trained, LogisticRegression):
      if np.dot(model_trained.coef_, vectorized_sample) + model_trained.intercept_ < 1e-10:
        return

    assert sklearn_prediction == pysmt_prediction, f'Pysmt prediction does not match sklearn prediction. \n{dict_sample} \n{factual_sample}'
    assert sklearn_prediction != factual_prediction, 'Counterfactual and factual samples have the same prediction.'


  counterfactuals = [] # list of tuples (samples, distances)
  # In case no counterfactuals are found (this could happen for a variety of
  # reasons, perhaps due to non-plausibility), return a template counterfactual
  counterfactuals.append({
    'counterfactual_sample': {},
    'counterfactual_distance': np.infty,
    'interventional_sample': {},
    'interventional_distance': np.infty,
    'time': np.infty,
    'norm_type': norm_type})

  if 'MACE_MIP_OBJ' in approach_string:

    norm_type = norm_type + '_obj'

    if isinstance(model_trained, MLPClassifier):
      solved, counterfactual_sample = findCFE4MLP(model_trained, dataset_obj, factual_sample, norm_type, 0, 0,
                                                  epsilon=epsilon, preprocessing=preprocessing)
      assert solved is True
    else:
      solved, counterfactual_sample, _ = findCFE4Others(approach_string, model_trained, dataset_obj, factual_sample,
                                                        norm_type, epsilon=epsilon)
      assert preprocessing is None, "Preprocessing is currently supported only for MLP models."

    # Assert samples have correct prediction label according to sklearn model
    assertPrediction(counterfactual_sample, model_trained, dataset_obj)
    counterfactual_distance = normalizedDistance.getDistanceBetweenSamples(
      factual_sample,
      counterfactual_sample,
      norm_type.replace('_obj', ''),
      dataset_obj)

    counterfactuals.append({
      'counterfactual_sample': counterfactual_sample,
      'counterfactual_distance': counterfactual_distance,
      'time': None,
      'norm_type': norm_type})

    if 'DIVERSE' in approach_string:
      assert(isinstance(model_trained, MLPClassifier))
      diverse_counterfactuals = []
      diverse_counterfactuals.append(counterfactual_sample) # closest
      done = False
      for i in range(k_cfes-1):
        if not done:
          solved, counterfactual_sample = findCFE4MLP(model_trained, dataset_obj, factual_sample, norm_type, 0, 0,
                                                      epsilon=epsilon, preprocessing=preprocessing,
                                                      diverse_cfs=diverse_counterfactuals)
        else:
          solved = True
          counterfactual_sample = diverse_counterfactuals[0]

        if solved is False:
          print(f"Only {i+1} diverse CFs found. Repeating the closest to minimize mean distance.")
          done = True
          counterfactual_sample = diverse_counterfactuals[0]

        diverse_counterfactuals.append(counterfactual_sample)
        assertPrediction(counterfactual_sample, model_trained, dataset_obj)
        counterfactual_distance = normalizedDistance.getDistanceBetweenSamples(
          factual_sample,
          counterfactual_sample,
          norm_type.replace('_obj', ''),
          dataset_obj)
        counterfactuals.append({
          'counterfactual_sample': counterfactual_sample,
          'counterfactual_distance': counterfactual_distance,
          'time': None,
          'norm_type': norm_type})


  elif 'MACE_MIP_EXP' in approach_string:

    #############################################
    ###### Reverse BS (Exponential Growth) ######
    #############################################

    reverse_norm_threshold = epsilon
    solved = False
    rev_bs_cfe, mip_model = None, None
    iteration_start_time, iteration_end_time = 0, 0

    while (not solved):
      norm_lower_bound = reverse_norm_threshold / 2.0 if reverse_norm_threshold != epsilon else 0.0
      if norm_lower_bound > 1:
        raise Exception("Not possible!")

      if isinstance(model_trained, MLPClassifier):
        solved, sol = findCFE4MLP(model_trained, dataset_obj, factual_sample, norm_type, norm_lower_bound,
                                  reverse_norm_threshold, epsilon=epsilon, preprocessing=preprocessing)
      else:
        solved, sol, mip_model = findCFE4Others(approach_string, model_trained, dataset_obj, factual_sample, norm_type,
                                    norm_lower_bound, reverse_norm_threshold, mip_model=mip_model, epsilon=epsilon)
        assert preprocessing is None, "Preprocessing is currently supported only for MLP models."

      if solved:
        rev_bs_cfe = sol
      else:
        reverse_norm_threshold *= 2.0

    # The upper bound on distance
    norm_upper_bound = reverse_norm_threshold
    # The lower bound on distance
    norm_lower_bound = 0.0 if reverse_norm_threshold == epsilon else reverse_norm_threshold / 2.0

    curr_norm_threshold = (norm_lower_bound + norm_upper_bound) / 2.0
    first_iter = True

    #############################################
    ################ Normal BS ##################
    #############################################

    iters, max_iters = 0, 100

    while iters < max_iters and norm_upper_bound - norm_lower_bound >= epsilon:
      print(
        f'\tIteration #{iters:03d}: testing norm threshold {curr_norm_threshold:.6f} in range [{norm_lower_bound:.6f}, {norm_upper_bound:.6f}]...\t',
        end='', file=log_file)
      iters = iters + 1

      if not first_iter:  # In the first iter, only the CFE from the exponential part will be saved.

        if isinstance(model_trained, MLPClassifier):
          solved, sol = findCFE4MLP(model_trained, dataset_obj, factual_sample, norm_type, norm_lower_bound,
                                    curr_norm_threshold, epsilon=epsilon, preprocessing=preprocessing)
        else:
          solved, sol, mip_model = findCFE4Others(approach_string, model_trained, dataset_obj, factual_sample, norm_type,
                                      norm_lower_bound, curr_norm_threshold, mip_model=mip_model, epsilon=epsilon)
      else:
        assert solved, 'last iter of reverse BS must have had solved the formula!'
        assert rev_bs_cfe is not None, 'last iter of reverse BS must have solved the formula!'

      if solved:  # There exists a counterfactual explanation

        if first_iter is True:
          counterfactual_sample = rev_bs_cfe
          first_iter = False
        else:
          counterfactual_sample = sol

        print('solution exists & found.', file=log_file)

        # Assert samples have correct prediction label according to sklearn model
        assertPrediction(counterfactual_sample, model_trained, dataset_obj)

        counterfactual_distance = normalizedDistance.getDistanceBetweenSamples(
          factual_sample,
          counterfactual_sample,
          norm_type,
          dataset_obj)
        counterfactuals.append({
          'counterfactual_sample': counterfactual_sample,
          'counterfactual_distance': counterfactual_distance,
          'time': None,
          'norm_type': norm_type})

        norm_lower_bound = norm_lower_bound
        # norm_upper_bound = curr_norm_threshold
        norm_upper_bound = float(counterfactual_distance + epsilon / 100)  # not float64
        curr_norm_threshold = getCenterNormThresholdInRange(norm_lower_bound, norm_upper_bound)

      else:  # no solution found in the assigned norm range --> update range and try again
        print('no solution exists.', file=log_file)
        norm_lower_bound = curr_norm_threshold
        norm_upper_bound = norm_upper_bound
        curr_norm_threshold = getCenterNormThresholdInRange(norm_lower_bound, norm_upper_bound)

  else:
    raise Exception(f"{approach_string} not a recognized approach.")

  closest_counterfactual_sample = sorted(counterfactuals, key=lambda x: x['counterfactual_distance'])[0]

  return counterfactuals, closest_counterfactual_sample


def getPrettyStringForSampleDictionary(sample, dataset_obj):

  if len(sample.keys()) == 0 :
    return 'No sample found.'

  key_value_pairs_with_x_in_key = {}
  key_value_pairs_with_y_in_key = {}
  for key, value in sample.items():
    if key in dataset_obj.getInputAttributeNames('kurz'):
      key_value_pairs_with_x_in_key[key] = value
    elif key in dataset_obj.getOutputAttributeNames('kurz'):
      key_value_pairs_with_y_in_key[key] = value
    else:
      raise Exception('Sample keys may only be `x` or `y`.')

  assert \
    len(key_value_pairs_with_y_in_key.keys()) == 1, \
    f'expecting only 1 output variables, got {len(key_value_pairs_with_y_in_key.keys())}'

  all_key_value_pairs = []
  for key, value in sorted(key_value_pairs_with_x_in_key.items(), key = lambda x: int(x[0][1:].split('_')[0])):
    all_key_value_pairs.append(f'{key} : {value}')
  all_key_value_pairs.append(f"{'y'}: {key_value_pairs_with_y_in_key['y']}")

  return f"{{{', '.join(all_key_value_pairs)}}}"


def genExp(
  explanation_file_name,
  model_trained,
  dataset_obj,
  factual_sample,
  norm_type,
  approach_string,
  epsilon,
  preprocessing,
  k_cfes):

  # # ONLY TO BE USED FOR TEST PURPOSES ON MORTGAGE DATASET
  # factual_sample = {'x0': 75000, 'x1': 25000, 'y': False}

  if 'MACE_MIP_OBJ' not in approach_string and 'MACE_MIP_EXP' not in approach_string:
    raise Exception(f'`{approach_string}` not recognized as valid approach string; expected `mint` or `mace`.')

  if DEBUG_FLAG:
    log_file = sys.stdout
  else:
    log_file = open(explanation_file_name, 'w')

  print('\n\n==============================================\n\n', file = log_file)

  # factual_sample['y'] = False
  start_time = time.time()

  # find closest counterfactual sample from this negative sample
  all_counterfactuals, closest_counterfactual_sample = findClosestCounterfactualSample(
    model_trained,
    dataset_obj,
    factual_sample,
    norm_type,
    approach_string,
    epsilon,
    log_file,
    preprocessing,
    k_cfes
  )

  end_time = time.time()

  print('\n', file = log_file)
  print(f"Factual sample: \t\t {getPrettyStringForSampleDictionary(factual_sample, dataset_obj)}", file = log_file)

  print(f"Nearest counterfactual sample:\t {getPrettyStringForSampleDictionary(closest_counterfactual_sample['counterfactual_sample'], dataset_obj)} (verified)", file = log_file)
  print(f"Minimum counterfactual distance: {closest_counterfactual_sample['counterfactual_distance']:.6f}", file = log_file)

  if 'DIVERSE' in approach_string:
    if not all_counterfactuals[0]['counterfactual_sample']:
      all_counterfactuals.remove(all_counterfactuals[0])
    else:
      raise Exception("First CF must be template.")

    assert len(all_counterfactuals) == k_cfes, f"Only {len(all_counterfactuals)} Diverse CFEs found. (< {k_cfes})"

    mean_proximity = normalizedDistance.getMeanProximity(all_counterfactuals, k_cfes)
    mean_diversity = normalizedDistance.getMeanDiversity(all_counterfactuals, k_cfes, norm_type, dataset_obj)

    return {
      'fac_sample': factual_sample,
      'cfe_found': True,
      'cfe_plausible': True,
      'cfe_time': end_time - start_time,
      'cfe_sample': closest_counterfactual_sample['counterfactual_sample'],
      'cfe_distance': closest_counterfactual_sample['counterfactual_distance'],
      'mean_proximity': mean_proximity,
      'mean_diversity': mean_diversity,
      'num_cfs': k_cfes,
      'all_counterfactuals': all_counterfactuals
    }
  else:
    return {
      'fac_sample': factual_sample,
      'cfe_found': True,
      'cfe_plausible': True,
      'cfe_time': end_time - start_time,
      'cfe_sample': closest_counterfactual_sample['counterfactual_sample'],
      'cfe_distance': closest_counterfactual_sample['counterfactual_distance'],
      'all_counterfactuals': all_counterfactuals
    }
