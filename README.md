# k8s-sidecar-rollout

k8s-sidecar-rollout aims to help avoid configuration drift when using sidecar injectors
by triggering rollout (restart) of all workloads that have one or more named (sidecar) containers.

## Background

When using for example istio, a sidecar container named `istio-proxy` is injected when Pods are created by using a MutatingWebhook.

The problem is that sidecar injectors do not keep state or have their own re-conciliation loop. And therefore changing
sidecar configurations do not trigger updates on already running Pods. New configuration is only applied when a Pod is restarted for whatever reason.

This configuration drift means that there are possibly breaking changes that will suddenly appear when a Pod is restarted some time in the future.

In addition, often sidecar injection is managed by one or more different teams than the teams deploying workloads.

So in effect a Deployment belonging to Team A might suddenly be unable to re-create Pods as normal when a node fails for example, and without Team A doing or even knowing about it. Since some time ago a breaking change was done to sidecar injection by Team B.

## Usage

Uses https://github.com/kubernetes-client/python

> PS: Use `kubectl version` to get cluster version. Then refer to https://pypi.org/project/kubernetes/#history to find the right client version

    pip3 install kubernetes==21.7.0
    pip3 install -r requirements.txt

Perform a dry run showing which Deployments contains Pods with a container named `istio-proxy`:

    python3 sidecar-rollout.py --sidecar-container-name=istio-proxy

Actually run rollout, not just dry-run:

    --confirm=true

### Options

Matching multiple sidecars:

    --sidecar-container-name=istio-proxy --sidecar-container-name=my-other-sidecar 

Including StatefulSets and DaemonSets (excluded by default):

    --include-daemonset=true --include-statefulset=true

Exclude Deployments (included by default):

    --exclude-deployments=true

Timeout for waiting for rollout to become Ready (default 300s):

    --timeout=600s

Set annotation prefix:

    --annotation-prefix=myCompany

Run multiple rollouts in parallel:

    --parallel-rollouts 10

Exclude namespace(s):

    --exclude-namespace=kube-system

Only rollout namespace(s):

    --include-namespace=my-first-app --include-namespace=my-other-app

Debug log:

    --log-level=debug

Log in JSON format:

    --log-format=json

