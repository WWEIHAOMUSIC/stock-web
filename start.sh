#!/bin/bash
# 股票Web查询系统 - 快速启动脚本
cd "$(dirname "$0")"
echo "📊 启动股票查询Web服务..."
echo "🌐 访问地址: http://localhost:5001"
echo "📱 支持手机浏览器访问（响应式布局）"
echo ""
python3 app/server.py
