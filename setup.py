from setuptools import setup, find_packages

setup(
    name="sipwav",
    version="0.1.2",
    packages=find_packages(),
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "sipcheck=sipwav.cli:main",
        ],
    },
    python_requires=">=3.10",
    install_requires=[
        "numpy>=1.24",
        "scipy>=1.10",
        "webrtcvad>=2.0",
        "soundfile>=0.12",
        "setuptools>=68,<81",  # pkg_resources（webrtcvad 依赖，82+ 已移除）
    ],
    extras_require={
        "full": [
            "librosa>=0.10",
            "torch>=2.0",
            "torchaudio>=2.0",  # funasr 依赖
            "funasr>=1.0",
            "modelscope>=1.0",
            "dashscope>=1.20",
        ],
        "server": [
            "dashscope>=1.20",
        ],
    },
)
