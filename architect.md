Architect: Agentic CAV Cyberattack Detection System

1. Project Vision
To build a cyberattack detection system for Connected and Autonomous Vehicles (CAV) based on Agentic AI.
Core Concept: Decouple the detection process into two layers: "Perception" and "Reasoning."
Perception Layer (The Sense): Utilizes Machine Learning models for rapid probabilistic scanning of individual BSM (Basic Safety Messages).
Reasoning Layer (The Agent): Combines vehicle historical memory, laws of physics, and multi-source data consistency to perform deep verification and final adjudication of alerts from the perception layer.

2. Technical Stack
Language: Python 3.10+
Data: VeReMi Extension Dataset (Balanced CSV for training, Raw CSV for testing).
Core ML: Scikit-learn (RandomForest / XGBoost) for base detection.
Agent Logic: Modular State-based Reasoning (Memory-driven).
Validation: Pytest for unit testing physical constraints.

3. System Architecture
3.1 Folder Structure
/data:
    /processed: Balanced datasets (for training the base detector).
    /raw: Original imbalanced datasets (for Agent stress testing/evaluation).
/models: Stored trained .pkl models and scalers.
/src:
    /perception: Low-level sensing (feature engineering, model training, real-time scoring).
    /reasoning: Agent core (memory.py buffer, physics_tool.py validator, agent_logic.py decision center).
    /simulation: Offline streaming simulator to replay CSV data as real-time message flows.
/tests: Unit tests for physical tools and Agent logic.
3.2 Component Responsibilities
Base Detector: Identifies statistical anomalies (e.g., outliers in RSSI vs. position).
Trajectory Memory: Buffers the last $N$ seconds of state for each sender_id to build spatio-temporal trajectories.
Physics Validator:
    Calculates if $\Delta position / \Delta time$ matches the speed claimed in the BSM.
    Checks if acceleration exceeds vehicle physical limits ($< 9 m/s^2$).

4. Key Logic Flows
4.1 Detection & Reasoning Loop
Input: Receive a new BSM message.
Step A: Base Detector provides an initial anomaly score $S$ ($0 \le S \le 1$).
Step B: If $S$ exceeds a threshold, the Agent is triggered to call the Physics Validator.
Step C: Agent retrieves the historical trajectory from Memory to check for spatio-temporal consistency.
Final Decision:
    Confirmed: High score + Physics violation -> Mark as ATTACK with specific reasoning.
    Suspicious: High score but physics plausible -> Mark as WARNING, continue observing.
    Normal: All indicators within normal bounds.

5. Development Phases
Phase 1: Data Preview & EDA (using balanced Zenodo data).
Phase 2: Train and save the Perception Layer classification model.
Phase 3: Develop Physics Tools and Trajectory Memory module (verified by unit tests).
Phase 4: Integrate Agent logic and perform replay testing on raw imbalanced data.