import copy
import numpy as np
np.random.seed(54321)

mu_x, sigma_x = 0, 1 # mean and standard deviation for data
mu_w, sigma_w = 0, 1 # mean and standard deviation for weights
n = 1000
d = 3

def getExperimentParams():
  w = np.random.normal(mu_w, sigma_w, (d, 1))

  X_train = np.random.normal(mu_x, sigma_x, (n, d))
  X_train = processDataAccordingToGraph(X_train)
  y_train = (np.sign(np.dot(X_train, w)) + 1) / 2

  X_test = np.random.normal(mu_x, sigma_x, (n, d))
  X_test = processDataAccordingToGraph(X_test)
  y_test = (np.sign(np.dot(X_test, w)) + 1) / 2

  return w, X_train, y_train, X_test, y_test

def processDataAccordingToGraph(data):
  # We assume the model below
  # X_1 := U_1 \\
  # X_2 := X_1 + 1 + U_2 \\
  # X_3 := (X_1 - 1) / 4 + np.sqrt{3} * X_2 + U_3
  # U_i ~ \forall ~ i \in [3] \sim \mathcal{N}(0,1)
  data = copy.deepcopy(data)
  data[:,0] = data[:,0]
  data[:,1] += data[:,0] + np.ones((n))
  data[:,2] += (data[:,0] - 1)/4 + np.sqrt(3) * data[:,1]
  return data

def ell2(a, b):
  np.linalg.norm(a - b, 2)