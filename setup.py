import os
from setuptools import setup, find_packages


with open(os.path.join("src", "cappr", "__init__.py")) as f:
    for line in f:
        if line.startswith("__version__ = "):
            version = str(line.split()[-1].strip('"'))
            break


requirements_base = [
    "numpy>=1.21.0",
    "tqdm>=4.27.0",
]

requirements_openai = [
    "openai>=0.26.0",
    "tiktoken>=0.2.0",
]

requirements_huggingface = [
    "sentencepiece>=0.1.99",  # for Llama tokenizers. cappr should work out-of-the-box
    "torch>=1.12.1",
    "transformers>=4.31.0",  # high version b/c Llama
]

requirements_huggingface_dev = [
    req if not req.startswith("transformers>=") else "transformers>=4.34.0"
    # To test Mistral in our testing workflow, we need this update
    for req in requirements_huggingface
] + ["huggingface-hub>=0.16.4"]

requirements_demos = [
    "datasets>=2.10.0",
    "jupyter>=1.0.0",
    "pandas>=1.5.3",
    "scikit-learn>=1.2.2",
]

requirements_dev = [
    "black>=23.1.0",
    "docutils<0.19",
    "pydata-sphinx-theme>=0.13.1",
    "pytest>=7.2.1",
    "pytest-cov>=4.0.0",
    "sphinx>=6.1.3",
    "sphinx-togglebutton>=0.3.2",
    "sphinxcontrib-napoleon>=0.7",
    "twine>=4.0.2",
]


with open("README.md", mode="r", encoding="utf-8") as f:
    readme = f.read()


setup(
    name="cappr",
    version=version,
    description="Get your LLM to pick the right category.",
    long_description=readme,
    long_description_content_type="text/markdown",
    url="https://github.com/kddubey/cappr/",
    license="Apache License 2.0",
    python_requires=">=3.8.0",
    install_requires=requirements_base + requirements_openai,
    extras_require={
        "hf": requirements_huggingface,
        "demos": requirements_huggingface + requirements_demos,
        "dev": requirements_huggingface_dev + requirements_demos + requirements_dev,
    },
    author_email="kushdubey63@gmail.com",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
)
