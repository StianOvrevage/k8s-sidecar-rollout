# k8s-sidecar-rollout

k8s-sidecar-rollout aims to help avoid configuration drift when using sidecar injectors
by triggering rollout (restart) of all workloads that have one or more named (sidecar) containers.

> Huge shout out to the team at [Signicat](https://www.signicat.com) for letting me share this small, but hopefully usefull tool and analysis with the rest of the Kubernetes community. If this is the kind of stuff that excites you head over to [Signicat Careers](https://www.signicat.com/about/careers) and join us!

# Background

> I've written [this blog post](https://chipmunk.no/blog/2022-05-03-kubernetes-sidecar-config-drift/) with more detailed explanation of the contributing factors to the problem.

When using for example istio, a sidecar container named `istio-proxy` is injected when Pods are created by using a MutatingWebhook.

The problem is that sidecar injectors do not keep state or have their own re-conciliation loop. And therefore changing
sidecar configurations do not trigger updates on already running Pods. New configuration is only applied when a Pod is restarted for whatever reason.

This configuration drift means that there are possibly breaking changes that will suddenly appear when a Pod is restarted some time in the future.

In addition, often sidecar injection is managed by one or more different teams than the teams deploying workloads.

So in effect a Deployment belonging to Team A might suddenly be unable to re-create Pods as normal when a node fails for example, and without Team A doing or even knowing about it. Since some time ago a breaking change was done to sidecar injection by Team B.

# Setup

Prerequisites:

    # Linux and WSL
    sudo apt-get install -y pkg-config libcairo2-dev

    # MacOS
    brew install cmake pkg-config cairo
    pip3 install pycairo

Uses https://github.com/kubernetes-client/python

> PS: Use `kubectl version` to get cluster server version. Then refer to https://pypi.org/project/kubernetes/#history to find the right client version to use here:

    pip3 install kubernetes==21.7.0
    pip3 install -r requirements.txt

# Usage

Perform a dry run showing which Deployments contains Pods with a container named `istio-proxy`:

    python3 sidecar-rollout.py --sidecar-container-name=istio-proxy

Actually run rollout, not just dry-run:

    --confirm=true

## Options

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

Only rollout workloads with Pods that are started before a time (for example when sidecar config was updated):

    --only-started-before="2022-05-01 13:00"

Exclude namespace(s):

    --exclude-namespace=kube-system

Only rollout namespace(s):

    --include-namespace=my-first-app --include-namespace=my-other-app

Debug log:

    --log-level=debug

Log in JSON format:

    --log-format=json

# How it works

We trigger Kubernetes reconciliation by adding a JSON patch with two new/updated annotations on the Pod spec of the workload:

    spec:
      template:
        metadata:
          annotations:
            sidecarRollout.rollout.timestamp: 2022-05-03T18:05:31
            sidecarRollout.rollout.reason: Update sidecars istio-proxy

> The nice thing about this is that if a product team gets surprised by a Pod restart they can rule out or confirm if the reason was a sidecar-rollout.

Workload selection:

  - Get all Pods for all namespaces
  - Ignore Pods inside/outside `--include-namespace` / `--exclude-namespace`
  - Ignore Pods that does not contain any of `--sidecar-container-name` containers
  - Ignore Pods that are started after `--only-started-before` (default is time when script runs)
  - Get the Owner for Pod (Deployment, StatefulSet, DaemonSet)
  - Ignore workloads depending on `--exclude-deployment`, `--include-statefulset`, `--include-daemonset`

Workloads not ignored gets added to an update queue.

1 update worker (or more, if defined by `--parallel-rollouts`) starts pulling workloads (Kind/Namespace/Name) from the queue and applies the patch. It then waits for `kubectl rollout status` to complete for `300s`, unless overridden with `--timeout`.

Results from a rollout is put in another queue. After the update queue is empty the results and messages are read from the queue and logged to the console.

# Goals

Goals:
  - Support Deployments, StatefulSets and DaemonSets using the same process.
  - Update while adhering to configured updateStrategy, podDisruptionBudgets, etc.
  - Simple filters for targeting sets of workloads.
  - Run to completion without interaction or crashing and log any anomalies that may require follow up after rolling out everything.

Anti-goals:
  - Re-invent logic to determine success/failure of a rollout.
  - Re-invent logic to determine which Pods to include/exclude based on whichever annotations and rules sidecar injectors use to target Pods for injection.

# Development

Update pip module versions:

    pip install pur
    pur -r requirements.txt

Beware that `urrlib3` may have to be set back to `urllib3<2.0` due to the `kubernetes` package.

# Backlog

Nice-to-haves for the future:

 - Workload selection:
    - Include based on annotation
    - Exclude based on annotation
    - Revisions:
      - Add revision annotation. Quickly gets complicated when matching on multiple arbitrary containers in one run
 - Display & Logging:
    - WARN for rollout status timeouts as they MAY succeed eventually
    - Awareness of Pods/workloads that are already in a crashed state and log differently
    - Capture output from kubectl and log in the same format (with timestamp, optionally JSON)
 - Interactive confirm-each option
 - Input validation
 - Graceful termination
 - Docker deployment
