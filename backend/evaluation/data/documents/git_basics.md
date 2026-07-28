# Git 基础

## 仓库初始化

`git init` 用于在当前目录创建一个新的 Git 仓库。执行后会在目录下生成一个 `.git` 隐藏目录，用于保存版本历史。

```bash
git init my-project
cd my-project
```

## 提交更改

Git 的工作流程通常包括三个阶段：工作区、暂存区和仓库。使用 `git add` 把修改加入暂存区，使用 `git commit` 把暂存区的内容提交到仓库。

```bash
git add README.md
git commit -m "添加 README"
```

## 分支管理

分支是 Git 中并行开发的基础。使用 `git branch` 查看或创建分支，使用 `git checkout` 或 `git switch` 切换分支。

```bash
git branch feature-x
git switch feature-x
```

合并分支时可以使用 `git merge`，如果存在冲突需要手动解决。
