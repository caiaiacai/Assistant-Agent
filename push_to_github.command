#!/bin/bash
cd "$(dirname "$0")"

echo "=== Assistant-Agent → GitHub ==="
git init
git add .
git commit -m "Initial commit: Assistant-Agent v1.0" 2>/dev/null || echo "(已有commit，跳过)"
git branch -M main
git remote remove origin 2>/dev/null
git remote add origin https://github.com/caiaiacai/Assistant-Agent.git
echo ""
echo "请输入 GitHub 用户名和 Token："
git push -u origin main
echo ""
echo "完成！按任意键关闭..."
read -n 1
