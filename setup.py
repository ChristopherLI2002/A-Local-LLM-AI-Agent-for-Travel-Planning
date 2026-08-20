from setuptools import find_packages, setup

setup(
    name="local-llm-travel-agent",
    version="0.1.0",
    description="A local LLM AI agent for travel planning (Ollama + Trip.com)",
    packages=find_packages(),
    install_requires=[
        "ollama>=0.6.0",
        "playwright>=1.40.0",
        "rich>=13.7.0",
        "python-dotenv>=1.0.0",
    ],
    entry_points={
        "console_scripts": [
            "travel-agent=travel_agent.cli:main",
        ],
    },
    python_requires=">=3.10",
)
