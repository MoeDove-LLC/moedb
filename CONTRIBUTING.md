# 投稿指南

任何用户都可以提交 Pull Request。自动检查通过不代表对象归属已经确认，也不保证合并；最终决定由仓库管理员作出。

## 1. 准备对象

从 `templates/` 复制所需模板，每个文件只填写一个对象，并放入对应目录：

| 对象 | 路径示例 | `source` |
|---|---|---|
| `as-set` | `data/as-set/AS64496~AS-CUSTOMERS` | `MDNIC` |
| `person` | `data/person/JDOE-AP` | 所属 RIR |
| `role` | `data/role/NOC-AP` | 所属 RIR |
| `route` | `data/route/192.0.2.0_24__AS64496` | `MDNIC` |
| `route6` | `data/route6/2001~db8~~_32__AS64496` | `MDNIC` |

对象文件没有 `.rpsl` 后缀。属性名、ASN、NIC handle 和 source 使用规范大小写。不要填写 `mnt-by`；发布任务会使用管理员配置的 maintainer 自动注入。

文件名从对象主键生成：AS-SET 中的 `:` 写成 `~`；route 中的 `/` 写成 `_`，IPv6 的 `:` 写成 `~`，最后追加 `__<origin>`。

## 2. 联系人与自动授权

每个数据 PR 的所有修改前、修改后对象必须只涉及一个联系人 handle，`person` 与 `role` 均可作为联系人。

每个 `as-set`、`route` 和 `route6` 都必须同时包含：

```rpsl
admin-c:       YOUR-HANDLE-RIR
tech-c:        YOUR-HANDLE-RIR
```

两个属性必须指向同一个本地联系人。首次新增联系人时，CI 自动使用该 PR 作者不可变的 GitHub 数字 ID 作为 owner；投稿人不填写用户名、数字 ID 或 `owner:`。以后 owner 可以直接引用未修改的既有联系人，联系人文件不需要在每个 PR 中重复修改。

修改和删除会同时检查修改前的联系人，因此不能把 A 的对象改挂到 B，也不能由另一个 GitHub 账户修改或删除。删除联系人前，PR 的最终仓库中不得再有对象引用它；删除后以同一 handle 重建仍归原 owner。一个 PR 如需涉及多个联系人，请拆分提交。

授权只使用 PR 作者的 GitHub 数字 ID，不使用用户名、commit 作者或邮箱，所以 GitHub 改名不会失去权限。首次绑定仍由管理员是否合并该 PR 决定。

## 3. 填写 source

`person` 和 `role` 的 source 只能是以下五个值之一：

- `AFRINIC`
- `APNIC`
- `ARIN`
- `LACNIC`
- `RIPE`

选择实际登记该 handle 的 RIR。CI 会在对应 RIR 的官方 RDAP 服务精确查询 NIC handle；查询成功只证明记录存在，不证明 PR 作者拥有该联系人或相关网络资源。

`as-set`、`route` 和 `route6` 的 source 必须为 `MDNIC`。

## 4. 填写 changed

`changed` 格式为：

```rpsl
changed:       contributor@example.net YYYYMMDD
```

邮箱可以使用投稿人的邮箱。日期必须等于该文件在 PR 分支中最后修改 commit 的 UTC 日期，不得与 Git 记录不一致、填写未来日期，并且每个对象只能有一个 `changed`。发布到 RADB 时会改用 GitHub 生成的 squash commit 日期。

## 5. 提交 Pull Request

数据投稿只修改 `data/`，不要同时修改工作流、校验脚本、模板或文档。请在 PR 模板中提供 ASN/前缀关系说明；联系人和 RIR 查询由 CI 从对象中自动确定。

不要提交 `owner:`、`mnt-by:`、`password:`、`auth:`、`override:`、`api-key:`、`delete:`、服务端生成字段或任何秘密；owner 完全由 CI 确定，`mnt-by` 完全由发布任务确定。`person` 和 `role` 会公开，请只填写获准公开的联系资料。

PR 检查会验证仓库格式、单一授权联系人、GitHub owner、引用关系、source、changed 日期及 RIR handle。管理员仍会人工核对资源关系后决定是否合并。

合并后，发布任务只在内存中为新增/更新对象注入管理员配置的 `mnt-by`，将 source 改为 `RADB`，并将 changed 改为管理员配置的发布邮箱和合并 commit 的 UTC 日期；RADB 可能把其中日期规范化为实际落库日。删除按 RADB 要求保留远端对象原文并追加 `delete`。Git 中保留投稿时的原值。
