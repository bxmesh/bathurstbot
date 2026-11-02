# CLAUDE.md — bxmesh/bathurstbot

BathurstBot, BxMesh's MeshCore bot (meshcore-bot and our plugins). Read
[`zindello-cluster/cluster-docs`](https://github.com/zindello-cluster/cluster-docs) before doing
anything here: its `CLAUDE.md`, the spec (`spec/design.md`), the binding layout (`spec/layout.md`)
and the rules (`spec/rules.md`). The rules apply here in full: an agent prepares a pull request and
never merges it or syncs in Argo. BxMesh never deploys itself (O14). meshcore-bot's release is the
one the owner approved; a new one is a pull request with its changelog, risks and rollback.

This repo is public. Never put a secret in it, nor the companion's address (it's on the owner's
LAN): both come from OpenBao at run time. Never make a real Workers AI call from a test.
