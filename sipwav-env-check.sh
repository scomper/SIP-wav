#!/bin/bash
# SIP-wav CentOS 7 环境兼容性评估脚本
# 用法: ssh -i <key> root@<host> 'bash -s' < sipwav-env-check.sh

set -e

echo "=========================================="
echo "SIP-wav 环境兼容性评估报告"
echo "生成时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}✅ $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }

TOTAL=0
PASS=0
FAIL=0
WARN=0

check() {
    local name="$1"
    local cmd="$2"
    local expect="$3"
    
    TOTAL=$((TOTAL + 1))
    if eval "$cmd" >/dev/null 2>&1; then
        pass "$name"
        PASS=$((PASS + 1))
    else
        fail "$name"
        FAIL=$((FAIL + 1))
    fi
}

check_ver() {
    local name="$1"
    local cmd="$2"
    local min_ver="$3"
    local recommended="$4"
    
    TOTAL=$((TOTAL + 1))
    local ver=$($cmd 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)
    if [ -z "$ver" ]; then
        fail "$name: 未安装"
        FAIL=$((FAIL + 1))
    elif [ "$(printf '%s\n' "$min_ver" "$ver" | sort -V | head -1)" = "$min_ver" ] && [ "$ver" != "$min_ver" ]; then
        # ver > min_ver
        if [ -n "$recommended" ] && [ "$ver" != "$recommended" ]; then
            warn "$name: $ver (推荐 $recommended)"
            WARN=$((WARN + 1))
        else
            pass "$name: $ver"
            PASS=$((PASS + 1))
        fi
    else
        # ver <= min_ver
        if [ -n "$recommended" ]; then
            fail "$name: $ver (需要 >= $min_ver, 推荐 $recommended)"
        else
            fail "$name: $ver (需要 >= $min_ver)"
        fi
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "========== 系统信息 =========="
echo "OS: $(cat /etc/os-release | grep PRETTY_NAME | cut -d'"' -f2)"
echo "Kernel: $(uname -r)"
echo "Arch: $(uname -m)"
echo "Memory: $(free -h | awk '/Mem:/{print $2}')"
echo "Disk: $(df -h / | awk 'NR==2{print $2 " total, " $4 " free"}')"

echo ""
echo "========== 基础组件 =========="

check_ver "OpenSSL" "openssl version" "1.1.1" "1.1.1w"
check_ver "Python 3" "python3 --version" "3.10" "3.12"
check_ver "GCC" "gcc --version" "4.8" ""
check_ver "GLIBC" "ldd --version" "2.17" ""

check "git 已安装" "command -v git"
check "curl 已安装" "command -v curl"
check "wget 已安装" "command -v wget"
check "make 已安装" "command -v make"

echo ""
echo "========== 音频库 =========="

check "libsndfile 已安装" "ldconfig -p | grep libsndfile"
check "libasound 已安装" "ldconfig -p | grep libasound"

if ! ldconfig -p | grep libsndfile >/dev/null 2>&1; then
    warn "libsndfile 安装方法: yum install -y libsndfile-devel"
fi

echo ""
echo "========== 网络连通性 =========="

check "PyPI 可达" "curl -s --connect-timeout 5 https://pypi.org"
check "GitHub 可达" "curl -s --connect-timeout 5 https://github.com"
check "阿里云 OSS 可达" "curl -s --connect-timeout 5 https://oss-cn-hangzhou.aliyuncs.com"

if ! curl -s --connect-timeout 5 https://github.com >/dev/null 2>&1; then
    warn "GitHub 不通，需要配置代理或使用 scp 传文件"
fi

echo ""
echo "========== Python 包管理 =========="

check "pip3 已安装" "command -v pip3"
check "venv 模块" "python3 -m venv --help"

if command -v pip3 >/dev/null 2>&1; then
    echo "已安装的包:"
    pip3 list 2>/dev/null | grep -E "numpy|librosa|scipy|soundfile" | head -5 || echo "  (无关键包)"
fi

echo ""
echo "========== 防火墙 =========="

if command -v firewall-cmd >/dev/null 2>&1; then
    echo "firewalld 状态: $(systemctl is-active firewalld 2>/dev/null || echo 'unknown')"
    if systemctl is-active firewalld >/dev/null 2>&1; then
        echo "开放端口:"
        firewall-cmd --list-ports 2>/dev/null | tr ' ' '\n' | head -10
    fi
elif command -v iptables >/dev/null 2>&1; then
    echo "iptables 规则数: $(iptables -L -n 2>/dev/null | wc -l)"
fi

echo ""
echo "=========================================="
echo "评估结果汇总"
echo "=========================================="
echo -e "总计: $TOTAL 项"
echo -e "${GREEN}通过: $PASS 项${NC}"
echo -e "${YELLOW}警告: $WARN 项${NC}"
echo -e "${RED}失败: $FAIL 项${NC}"

echo ""
echo "=========================================="
echo "兼容性风险矩阵"
echo "=========================================="

if [ "$FAIL" -eq 0 ] && [ "$WARN" -eq 0 ]; then
    echo -e "${GREEN}✅ 环境完全兼容，可直接部署${NC}"
elif [ "$FAIL" -eq 0 ]; then
    echo -e "${YELLOW}⚠️  环境基本兼容，有 $WARN 项需注意${NC}"
else
    echo -e "${RED}❌ 环境不兼容，有 $FAIL 项必须修复${NC}"
fi

echo ""
echo "=========================================="
echo "CentOS 7 已知踩坑清单"
echo "=========================================="
echo "1. OpenSSL 1.0.2 → 需编译 1.1.1w"
echo "2. Python 3.6.8 → 需源码编译 3.12"
echo "3. GCC 4.8.5 → numpy<2.0 锁版本"
echo "4. GLIBC 2.17 → Miniconda 不可用"
echo "5. GitHub 443 不通 → scp 或代理"
echo "6. libsndfile 缺失 → yum install"
echo "7. 终端 input() 错乱 → _prompt() + readline"
echo ""

echo "=========================================="
echo "建议安装顺序"
echo "=========================================="
echo "1. yum install -y git libsndfile-devel wget"
echo "2. 编译 OpenSSL 1.1.1w → /usr/local/ssl"
echo "3. 编译 Python 3.12 → /usr/local/bin/python3.12"
echo "4. python3.12 -m venv /opt/sipwav/venv"
echo "5. source /opt/sipwav/venv/bin/activate"
echo "6. pip install 'numpy<2.0' librosa soundfile"
echo "7. pip install -r requirements.txt"
echo "8. scp 项目文件到服务器（GitHub 不通）"
echo ""
