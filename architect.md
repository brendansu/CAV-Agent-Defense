Architecture: Agentic CAV Attack Detection via LLM Reasoning
1. Executive Summary
This project transitions from traditional "signal-in, result-out" perception models to a Reasoning-based Security Agent. The system leverages a Large Language Model (LLM) fine-tuned via LoRA (Low-Rank Adaptation) to act as a digital detective. Instead of simple classification, the agent analyzes the physical consistency and logical coherence of V2X (Vehicle-to-Everything) messages by comparing subjective observations (Vehicle Logs) with objective physical laws.

2. Core Components
A. Contextual Awareness Module (Data Orchestrator)
This module reconstructs the "worldview" of an individual vehicle from raw JSON logs.
    Self-State Integrator: Extracts type: 2 (GPS) data to establish the vehicle's own ground-truth position and trajectory.
    Neighbor Observation stream: Parses incoming type: 3 (BSM) messages, capturing reported coordinates, speeds, and RSSI (Received Signal Strength Indicator).
    Temporal Memory Buffer: Maintains a sliding window of previous states for each SenderID to detect sudden jumps or physical impossibilities over time.

B. The Reasoning Core (LoRA-tuned LLM)
The "brain" of the agent, trained to understand vehicular physics and communication patterns.
    Plausibility Checker: Evaluates if a reported position change is physically possible given the elapsed time ($\Delta Pos / \Delta t$).
    Cross-Modal Verification: Correlates spatial data with signal data (e.g., "Does this RSSI match the claimed 2km distance?").
    Inconsistency Resolver: Identifies "lying" nodes by detecting discrepancies between a vehicle's self-reported state and its transmitted data.

C. Agentic Action & Explanation Layer
    Chain-of-Thought (CoT) Output: The model generates a verbal justification for its decision before issuing a label.
    Adaptive Response: Based on the reasoning, the agent decides whether to ignore the message, flag the sender for observation, or broadcast a network-wide alert.

3. System Workflow
Observation Phase: The agent ingests a sequence of messages from a specific scenario.
Evidence Alignment: The pre-processing script aligns the "Truth" (Type 2/Ground Truth) with the "Claim" (Type 3) to create a comparative prompt.
Logical Inference: The LLM processes the prompt, looking for physical contradictions (e.g., constant offsets in A1 attacks).
Actionable Decision: The model outputs a structured response: [Reasoning] -> [Decision] -> [Confidence].
Evaluation: Results are validated against the groundtruth.json to measure Precision, Recall, and the robustness of the reasoning chain.

5. Technical Stack
    Data Source: VeReMi Original Dataset (JSON).
    Fine-tuning Technique: LoRA (Low-Rank Adaptation) for parameter-efficient learning.
    Reasoning Framework: Chain-of-Thought (CoT) prompting for explainable AI (XAI).
    Environment: Python-based data pipeline + HuggingFace PEFT library.