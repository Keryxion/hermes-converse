# Alpha deploys

Label a PR `alpha:sakura`, `alpha:penny` or `alpha:hermie` to deploy its head commit to that
Hermes host. Every later push to the PR deploys again while the label stays. Removing the
label stops further deploys; the host keeps what it last got. Fork PRs are not deployed.

1. `.github/workflows/alpha.yml` creates a GitHub deployment of the PR head to environment `<host>`.
2. Crow receives the deployment webhook and runs `.crow/alpha.yaml` on its docker agent.
3. That step ssh's to `hermes@<host>` with a key whose `authorized_keys` entry forces
   `~/.local/bin/hermes-converse-alpha` (this directory's `hermes-converse-alpha`).
4. The command fetches the commit into `~/.hermes/plugins/hermes-converse`, checks it out,
   restarts `hermes-gateway`, and goes back to the previous commit if
   `/v1/audio/converse` does not answer 400 within two minutes. It does not install
   changed `python_dependencies`; it warns instead.

The deployment, Crow pipeline and commit status show on the PR. Each host logs its
deploys to `~/.hermes/plugins/hermes-converse/.git/alpha-deploy.log`.

## Setup

- Crow secrets on `Keryxion/hermes-converse`, event `deployment`, image `alpine:3`:
  `alpha_ssh_key` (private key), `alpha_ssh_config` (`Host sakura penny hermie` with
  `HostName`, `User hermes`, `IdentityFile ~/.ssh/alpha`), `alpha_known_hosts`.
- On each host, install `hermes-converse-alpha` as `~/.local/bin/hermes-converse-alpha` and add
  `restrict,command="/home/hermes/.local/bin/hermes-converse-alpha" <public key>` to
  `~/.ssh/authorized_keys`. Reinstall it there when this script changes.
- Adding a host: a label, the `case` in `alpha.yml`, the `environment` list in
  `.crow/alpha.yaml`, the ssh config secret and known_hosts.
