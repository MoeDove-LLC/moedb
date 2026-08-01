# MOEDB

MOEDB 是一个面向客户自管理、由管理员审核并发布到 RADB 的 Git/RPSL 仓库。

仓库只接受 `as-set`、`person`、`role`、`route` 和 `route6`。

## 提交对象

1. Fork 本仓库。
2. 从 [`templates/`](templates/) 复制模板到对应的 `data/<类型>/` 目录并填写对象。
3. 提交变更；一个文件只放一个对象，一个 PR 只能涉及一个联系人 handle。
4. 创建 Pull Request，等待自动检查和管理员审核。

首次新增 `person` 或 `role` 时，CI 自动认定该 PR 作者的 GitHub 数字 ID 为长期 owner，不需要填写用户名、ID 或 `owner` 属性。以后 owner 可直接修改该联系人及引用它的对象，不需要每次修改联系人文件；GitHub 改名不影响授权。删除按修改前对象的联系人鉴权，A 不能修改 B 名下的对象。

`person` / `role` 使用其所属 RIR 的 source；其他对象使用 `MDNIC`。详细格式、文件名和 `changed` 日期规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

检查通过只表示格式和公开记录符合规则，是否合并始终由管理员决定。
