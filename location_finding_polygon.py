import mlflow
import mlflow.pytorch
import numpy as np
import pandas as pd
import pyro
import pyro.distributions as dist
import torch
from pyro.infer.util import torch_item
from torch import nn
from tqdm import trange

from contrastive.mi import PriorContrastiveEstimation
from experiment_tools.pyro_tools import auto_seed
from neural.modules import (
    BatchDesignBaseline,
    RandomDesignBaseline,
    SetEquivariantDesignNetwork,
)
from oed.design import OED
from oed.primitives import compute_design, latent_sample, observation_sample


class EncoderNetwork(nn.Module):
    """Encoder network for location finding example"""

    def __init__(self, design_dim, osbervation_dim, hidden_dim, encoding_dim):
        super().__init__()
        self.encoding_dim = encoding_dim
        self.design_dim_flat = design_dim[0] * design_dim[1]
        input_dim = self.design_dim_flat + osbervation_dim

        self.linear1 = nn.Linear(input_dim, hidden_dim)
        self.output_layer = nn.Linear(hidden_dim, encoding_dim)
        self.relu = nn.ReLU()
        self.softplus = nn.Softplus()

    def forward(self, xi, y, **kwargs):
        inputs = torch.cat([xi.flatten(-2), y], dim=-1)

        x = self.linear1(inputs)
        x = self.relu(x)
        x = self.output_layer(x)
        return x


class EmitterNetwork(nn.Module):
    """Emitter network for location finding example"""

    def __init__(self, encoding_dim, design_dim):
        super().__init__()
        self.design_dim = design_dim
        self.design_dim_flat = design_dim[0] * design_dim[1]
        self.linear = nn.Linear(encoding_dim, self.design_dim_flat)

    def forward(self, r):
        xi_flat = self.linear(r)
        return xi_flat.reshape(xi_flat.shape[:-1] + self.design_dim)


class HiddenObjects(nn.Module):
    """Location finding example"""

    def __init__(
        self,
        design_net,
        base_signal=0.1,  # G-map hyperparam
        max_signal=1e-4,  # G-map hyperparam
        theta_loc=None,  # prior on theta mean hyperparam
        theta_covmat=None,  # prior on theta covariance hyperparam
        noise_scale=None,  # this is the scale of the noise term
        n=1,
        p=1,  # physical dimension
        K=1,  # number of sources
        T=2,  # number of experiments
        lower_bound=torch.tensor([0, 0]),
        upper_bound=torch.tensor([1, 1]),
    ):
        super().__init__()
        self.design_net = design_net
        self.base_signal = base_signal
        self.max_signal = max_signal
        # Set prior:
        self.theta_loc = theta_loc if theta_loc is not None else torch.zeros((K, p))
        self.theta_covmat = theta_covmat if theta_covmat is not None else torch.eye(p)
        self.theta_prior = dist.MultivariateNormal(
            self.theta_loc, self.theta_covmat
        ).to_event(1)
        # Observations noise scale:
        self.noise_scale = noise_scale if noise_scale is not None else torch.tensor(1.0)
        self.n = n  # batch=1
        self.p = p  # dimension of theta (location finding example will be 1, 2 or 3).
        self.K = K  # number of sources
        self.T = T  # number of experiments
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound

    def forward_map(self, xi, theta):
        """Defines the forward map for the hidden object example
        y = G(xi, theta) + Noise.
        """
        mean_ys = []

        for i in range(xi.shape[-2]):
            xii = xi[..., i, :].unsqueeze(-2)

            # two norm squared
            sq_two_norm = (xii - theta).pow(2).sum(axis=-1)
            sq_two_norm_inverse = (self.max_signal + sq_two_norm).pow(-1)

            # sum over the K sources, add base signal and take log.
            mean_y = torch.log(
                self.base_signal + sq_two_norm_inverse.sum(-1, keepdim=True)
            )
            mean_ys.append(mean_y)

        return torch.stack(mean_ys, axis=-1).squeeze(
            -2
        )  # returns dimension [samples, n]

    def transform_designs(self, xi_untransformed):
        xi_prop = nn.Sigmoid()(xi_untransformed)
        xi = self.lower_bound + xi_prop * (self.upper_bound - self.lower_bound)
        return xi

    def model(self):
        if hasattr(self.design_net, "parameters"):
            pyro.module("design_net", self.design_net)

        ########################################################################
        # Sample latent variables theta
        ########################################################################
        theta = latent_sample("theta", self.theta_prior)
        y_outcomes = []
        xi_designs = []

        # T-steps experiment
        for t in range(self.T):
            ####################################################################
            # Get a design xi; shape is [num-outer-samples x n x p]
            ####################################################################
            xi_untransformed = compute_design(
                f"xi{t + 1}", self.design_net.lazy(*zip(xi_designs, y_outcomes))
            )
            # print("**")
            # print(xi.shape)
            # print(xi.squeeze(1).shape)

            # transform design into a square
            xi = self.transform_designs(xi_untransformed)

            ####################################################################
            # Sample y at xi; shape is [num-outer-samples x n]
            ####################################################################
            mean = self.forward_map(xi, theta)
            sd = self.noise_scale
            # TODO: is this multivariate now?
            # var = self.noise_scale * torch.eye(xi.shape[-2])
            # y = observation_sample(
            #     f"y{t + 1}", dist.MultivariateNormal(mean, var).to_event(1) ###TODO: .to_event(2)
            # )
            y = observation_sample(f"y{t + 1}", dist.Normal(mean, sd).to_event(1))

            y_outcomes.append(y)
            xi_designs.append(xi_untransformed)  #! pass untransformed

        return xi_designs, y_outcomes

    def forward(self, theta=None):
        """Run the policy"""
        self.design_net.eval()
        if theta is not None:
            model = pyro.condition(self.model, data={"theta": theta})
        else:
            model = self.model
        designs = []
        observations = []

        with torch.no_grad():
            trace = pyro.poutine.trace(model).get_trace()
            for t in range(self.T):
                xi = trace.nodes[f"xi{t + 1}"]["value"]
                designs.append(xi)

                y = trace.nodes[f"y{t + 1}"]["value"]
                observations.append(y)
        return torch.cat(designs).unsqueeze(1), torch.cat(observations).unsqueeze(1)

    def eval(self, n_trace=3, theta=None, verbose=True):
        """run the policy, print output and return in a pandas df"""
        self.design_net.eval()
        if theta is not None:
            model = pyro.condition(self.model, data={"theta": theta})
        else:
            model = self.model

        output = []
        true_thetas = []
        with torch.no_grad():
            for i in range(n_trace):
                trace = pyro.poutine.trace(model).get_trace()
                true_theta = trace.nodes["theta"]["value"].cpu()
                if verbose:
                    print("\nExample run {}".format(i + 1))
                    print(f"*True Theta: {true_theta}*")
                run_xis = []
                run_ys = []

                # Print optimal designs, observations for given theta
                for t in range(self.T):
                    xi_untransformed = trace.nodes[f"xi{t + 1}"]["value"].cpu()
                    # transform
                    xi = self.transform_designs(xi_untransformed)
                    run_xis.append(xi)
                    y = trace.nodes[f"y{t + 1}"]["value"].cpu()  # .item()
                    run_ys.append(y)
                    if verbose:
                        print(f"xi{t + 1}: {xi}")
                        print(f" y{t + 1}: {y}")

                run_df = pd.DataFrame(torch.cat(run_xis).numpy())
                run_df.columns = [f"xi_{i}" for i in range(self.p)]
                run_df["observations"] = np.array(run_ys).flatten()
                run_df["n"] = np.tile(range(self.n), self.T)
                run_df["order"] = np.repeat(range(1, self.T + 1), self.n)
                run_df["run_id"] = i + 1
                output.append(run_df)
                true_thetas.append(true_theta.numpy())

        run_df = pd.concat(output)

        if verbose:
            print(run_df)

        # save to self
        self.run_df = run_df
        self.true_thetas = true_thetas

        return run_df, true_thetas


class DAD:
    """
    seed:
    num_steps:
    num_inner_samples:  # L in denom
    num_outer_samples:  # N to estimate outer E
    lr:  # learning rate of adam optim
    gamma:  # scheduler for adam optim
    p:  # number of physical dim
    K:  # number of sources
    T:  # number of experiments
    noise_scale:
    base_signal:
    max_signal:
    device:
    hidden_dim:
    encoding_dim:
    mlflow_experiment_name:
    design_network_type:  # "dad" or "static" or "random"
    adam_betas_wd:
    n_trace=3:
    theta=None:
    verbose=True:
    """

    def __init__(
        self,
        seed=-1,
        num_steps=500,  # 10k
        num_inner_samples=100,  # 64
        num_outer_samples=200,  # 128
        lr=0.0001,
        gamma=0.95,
        device="cpu",
        p=2,
        K=2,
        T=5,
        n=1,
        noise_scale=0.5,
        base_signal=0.1,
        max_signal=1e-4,
        hidden_dim=128,
        encoding_dim=8,
        mlflow_experiment_name="test",
        design_network_type="dad",
        adam_betas_wd=[0.8, 0.998, 0],
        n_trace=3,
        theta=None,
        verbose=True,
        lower_bound=torch.tensor([0, 0]),
        upper_bound=torch.tensor([1, 1]),
    ):
        self.seed = seed
        self.num_steps = num_steps
        self.num_inner_samples = num_inner_samples
        self.num_outer_samples = num_outer_samples
        self.lr = lr
        self.gamma = gamma
        self.device = device
        self.p = p
        self.K = K
        self.T = T
        self.n = n
        self.noise_scale = noise_scale
        self.base_signal = base_signal
        self.max_signal = max_signal
        self.hidden_dim = hidden_dim
        self.encoding_dim = encoding_dim
        self.mlflow_experiment_name = mlflow_experiment_name
        self.design_network_type = design_network_type
        self.adam_betas_wd = adam_betas_wd
        self.n_trace = n_trace
        self.theta = theta
        self.verbose = verbose
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound

    def fit(self):
        pyro.clear_param_store()
        seed = auto_seed(self.seed)
        *adam_betas, adam_weight_decay = self.adam_betas_wd

        ### Set up model ###
        encoder = EncoderNetwork(
            (self.n, self.p), self.n, self.hidden_dim, self.encoding_dim
        )
        emitter = EmitterNetwork(self.encoding_dim, (self.n, self.p))

        # Design net: takes pairs [design, observation] as input
        if self.design_network_type == "static":
            design_net = BatchDesignBaseline(self.T, (self.n, self.p)).to(self.device)
        elif self.design_network_type == "random":
            design_net = RandomDesignBaseline(self.T, (self.n, self.p)).to(self.device)
            self.num_steps = 0  # no gradient steps needed
        elif self.design_network_type == "dad":
            design_net = SetEquivariantDesignNetwork(
                encoder, emitter, empty_value=torch.ones(self.n, self.p) * 0.01
            ).to(self.device)
        else:
            raise ValueError(
                f"design_network_type={self.design_network_type} not supported."
            )

        ### Set up Mlflow logging ###
        mlflow.set_experiment(self.mlflow_experiment_name)

        ## Reproducibility
        mlflow.log_param("seed", seed)

        ## Model hyperparams
        mlflow.log_param("base_signal", self.base_signal)
        mlflow.log_param("max_signal", self.max_signal)
        mlflow.log_param("noise_scale", self.noise_scale)
        mlflow.log_param("num_experiments", self.T)
        mlflow.log_param("num_sources", self.K)
        mlflow.log_param("physical_dim", self.p)

        ## Design network hyperparams
        mlflow.log_param("design_network_type", self.design_network_type)
        if self.design_network_type == "dad":
            mlflow.log_param("hidden_dim", self.hidden_dim)
            mlflow.log_param("encoding_dim", self.encoding_dim)
        mlflow.log_param("num_inner_samples", self.num_inner_samples)
        mlflow.log_param("num_outer_samples", self.num_outer_samples)

        ## Optimiser hyperparams
        mlflow.log_param("num_steps", self.num_steps)
        mlflow.log_param("lr", self.lr)
        mlflow.log_param("gamma", self.gamma)
        mlflow.log_param("adam_beta1", adam_betas[0])
        mlflow.log_param("adam_beta2", adam_betas[1])
        mlflow.log_param("adam_weight_decay", adam_weight_decay)

        ### Prior hyperparams ###
        # The prior is K independent * p-variate Normals. For example, if there's 1 source
        # (K=1) in 2D (p=2), then we have 1 bivariate Normal.
        theta_prior_loc = torch.zeros(
            (self.K, self.p), device=self.device
        )  # mean of the prior
        theta_prior_covmat = torch.eye(
            self.p, device=self.device
        )  # covariance of the prior
        # noise of the model: the sigma in N(G(theta, xi), sigma)
        noise_scale_tensor = self.noise_scale * torch.tensor(
            1.0, dtype=torch.float32, device=self.device
        )
        # fix the base and the max signal in the G-map
        ho_model = HiddenObjects(
            design_net=design_net,
            base_signal=self.base_signal,
            max_signal=self.max_signal,
            theta_loc=theta_prior_loc,
            theta_covmat=theta_prior_covmat,
            noise_scale=noise_scale_tensor,
            n=self.n,
            p=self.p,
            K=self.K,
            T=self.T,
            lower_bound=self.lower_bound,
            upper_bound=self.upper_bound,
        )

        ### Set-up optimiser ###
        optimizer = torch.optim.Adam
        # Annealed LR. Set gamma=1 if no annealing required
        scheduler = pyro.optim.ExponentialLR(
            {
                "optimizer": optimizer,
                "optim_args": {
                    "lr": self.lr,
                    "betas": adam_betas,
                    "weight_decay": adam_weight_decay,
                },
                "gamma": self.gamma,
            }
        )
        ### Set-up loss ###
        pce_loss = PriorContrastiveEstimation(
            self.num_outer_samples, self.num_inner_samples
        )
        oed = OED(ho_model.model, scheduler, pce_loss)

        ### Optimise ###
        loss_history = []
        num_steps_range = trange(0, self.num_steps, desc="Loss: 0.000 ")
        for i in num_steps_range:
            loss = oed.step()  #### TODO: option #2 change here (or in design.py)
            loss = torch_item(loss)
            loss_history.append(loss)
            # Log every 50 losses -> too slow (and unnecessary to log everything)
            if i % 50 == 0:
                num_steps_range.set_description("Loss: {:.3f} ".format(loss))
                loss_eval = oed.evaluate_loss()
                mlflow.log_metric("loss", loss_eval)
            # Decrease LR at every 1K steps
            if i % 1000 == 0:
                scheduler.step()

        # log some basic metrics: %decrease in loss over the entire run
        if len(loss_history) == 0:
            # this happens when we have random designs - there are no grad updates
            loss = torch_item(pce_loss.differentiable_loss(ho_model.model))
            mlflow.log_metric("loss", loss)
            mlflow.log_metric("loss_diff50", 0)
            mlflow.log_metric("loss_av50", loss)
        else:
            loss_diff50 = (
                np.mean(loss_history[-51:-1]) / np.mean(loss_history[0:50]) - 1
            )
            mlflow.log_metric("loss_diff50", loss_diff50)
            loss_av50 = np.mean(loss_history[-51:-1])
            mlflow.log_metric("loss_av50", loss_av50)

        ho_model.eval(n_trace=self.n_trace, theta=self.theta, verbose=self.verbose)

        # Store the results dict as an artifact
        mlflow.pytorch.log_model(ho_model.cpu(), "model")
        ml_info = mlflow.active_run().info
        model_loc = f"mlruns/{ml_info.experiment_id}/{ml_info.run_id}/artifacts/model"

        # save to self
        self.model_loc = model_loc
        self.experiment_id = ml_info.experiment_id
        print(f"Saved. The experiment-id of this run is {ml_info.experiment_id}")

        # must end run for status to change to finished
        mlflow.end_run()

        self.ho_model = ho_model

        return self
