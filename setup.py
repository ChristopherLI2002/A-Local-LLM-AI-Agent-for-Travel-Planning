from setuptools import find_packages, setup

setup(
    name="travel-agent",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "ollama>=0.6.0",
        "playwright>=1.40.0",
        "rich>=13.7.0",
        "python-dotenv>=1.0.0",
        "flask>=3.0.0",
        "markdown>=3.5.0",
        "bleach>=6.1.0",
    ],
    entry_points={
        "console_scripts": [
            "travel-agent=travel_agent.cli:main",
        ],
    },
    python_requires=">=3.10",
)
