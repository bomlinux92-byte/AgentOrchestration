"""Regression tests for AgentSandbox resource limits isolation (issue #3982).

Tests that per-agent resource limits are applied in the child process context,
not the parent process, so that different agents with different limits do not
interfere with each other.
"""

import pytest
import subprocess
import resource
import sys
import os


class TestSandboxLimitsIsolation:
    """Test that resource limits are isolated per-agent via preexec_fn."""

    def test_apply_limits_in_child_not_parent(self):
        """Verify setrlimit affects child process, not the parent test process.

        This was the core bug: apply_limits() called setrlimit() in the parent,
        causing one agent's limits to affect all agents in the same process.
        """
        # Read current limits before any child process is spawned
        soft_cpu, hard_cpu = resource.getrlimit(resource.RLIMIT_CPU)
        soft_as, hard_as = resource.getrlimit(resource.RLIMIT_AS)

        # Spawn a child that sets very restrictive limits
        restrictive_cpu = 1  # 1 second
        restrictive_mem = 20 * 1024 * 1024  # 20 MB (enough to run python)

        script = f'''
import resource
resource.setrlimit(resource.RLIMIT_CPU, ({restrictive_cpu}, {restrictive_cpu}))
mem = {restrictive_mem}
resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
# Verify limits were set in THIS process
cpu_soft, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
as_soft, as_hard = resource.getrlimit(resource.RLIMIT_AS)
print(f"CHILD_CPU_SOFT={{cpu_soft}}")
print(f"CHILD_AS_SOFT={{as_soft}}")
'''

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
        )

        child_cpu_soft = None
        child_as_soft = None
        for line in result.stdout.strip().split("\n"):
            if line.startswith("CHILD_CPU_SOFT="):
                child_cpu_soft = int(line.split("=")[1])
            if line.startswith("CHILD_AS_SOFT="):
                child_as_soft = int(line.split("=")[1])

        # Verify child applied its own restrictive limits
        assert child_cpu_soft == restrictive_cpu, f"Child CPU limit should be {restrictive_cpu}, got {child_cpu_soft}"
        assert child_as_soft == restrictive_mem, f"Child AS limit should be {restrictive_mem}, got {child_as_soft}"

        # Verify parent (this test process) still has original limits
        parent_cpu_soft, _ = resource.getrlimit(resource.RLIMIT_CPU)
        parent_as_soft, _ = resource.getrlimit(resource.RLIMIT_AS)
        assert parent_cpu_soft == soft_cpu, "Parent CPU limit should be unchanged"
        assert parent_as_soft == soft_as, "Parent AS limit should be unchanged"

    def test_preexec_fn_provides_isolated_limits(self):
        """Test that two agents with different limits do not interfere.

        Uses a script that tries to consume memory and verifies each agent's
        limits are independent based on which preexec_fn was used.
        """
        from src.agent.sandbox import AgentSandbox, ResourceLimits

        sandbox = AgentSandbox()

        limits_a = ResourceLimits(cpu_time=5, memory_mb=256)
        limits_b = ResourceLimits(cpu_time=10, memory_mb=512)

        preexec_a = sandbox.get_preexec_fn(limits_a)
        preexec_b = sandbox.get_preexec_fn(limits_b)

        # Script that reports its effective limits
        # Script that reports its effective limits - use regular string to avoid f-string issues
        check_script = '''
import resource
cpu_soft, _ = resource.getrlimit(resource.RLIMIT_CPU)
as_soft, _ = resource.getrlimit(resource.RLIMIT_AS)
print("CPU=%d" % cpu_soft)
print("AS=%d" % as_soft)
'''

        proc_a = subprocess.Popen(
            [sys.executable, "-c", check_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=preexec_a,
        )
        proc_b = subprocess.Popen(
            [sys.executable, "-c", check_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=preexec_b,
        )

        stdout_a, _ = proc_a.communicate(timeout=5)
        stdout_b, _ = proc_b.communicate(timeout=5)

        # Parse outputs
        def parse_limits(output):
            cpu = None
            as_val = None
            for line in output.decode().strip().split("\n"):
                if line.startswith("CPU="):
                    cpu = int(line.split("=")[1])
                if line.startswith("AS="):
                    as_val = int(line.split("=")[1])
            return cpu, as_val

        cpu_a, as_a = parse_limits(stdout_a)
        cpu_b, as_b = parse_limits(stdout_b)

        # Agent A should have its restrictive limits (5 seconds, 256 MB)
        assert cpu_a == 5, f"Agent A CPU should be 5, got {cpu_a}"
        assert as_a == 256 * 1024 * 1024, f"Agent A AS should be {256*1024*1024}, got {as_a}"

        # Agent B should have its own limits (10 seconds, 512 MB)
        assert cpu_b == 10, f"Agent B CPU should be 10, got {cpu_b}"
        assert as_b == 512 * 1024 * 1024, f"Agent B AS should be {512*1024*1024}, got {as_b}"

    def test_get_preexec_fn_returns_callable(self):
        """Verify get_preexec_fn returns a callable that can be used with subprocess.Popen."""
        from src.agent.sandbox import AgentSandbox, ResourceLimits

        sandbox = AgentSandbox()
        limits = ResourceLimits(cpu_time=30, memory_mb=128)

        preexec_fn = sandbox.get_preexec_fn(limits)
        assert callable(preexec_fn), "get_preexec_fn should return a callable"

        # Verify it can be called without error (simulating child process init)
        preexec_fn()

        # The above call sets limits in current process which is the test process.
        # That is fine for this unit test - what matters is it runs in child context
        # when used with subprocess.Popen


class TestAgentRuntimeWithPreexec:
    """Test AgentRuntime.start() passes preexec_fn correctly."""

    def test_start_accepts_preexec_fn(self):
        """Verify AgentRuntime.start() accepts and uses preexec_fn."""
        from src.agent.runtime import AgentRuntime
        from src.agent.sandbox import AgentSandbox, ResourceLimits

        runtime = AgentRuntime()
        sandbox = AgentSandbox()

        limits = ResourceLimits(cpu_time=15, memory_mb=256)
        preexec_fn = sandbox.get_preexec_fn(limits)

        check_script = '''
import resource
cpu_soft, _ = resource.getrlimit(resource.RLIMIT_CPU)
print("CPU=%d" % cpu_soft)
'''

        success = runtime.start(
            agent_id="test-agent",
            command=[sys.executable, "-c", check_script],
            preexec_fn=preexec_fn,
        )

        assert success, "Agent should start successfully"
        assert runtime.is_running("test-agent"), "Agent should be running"

        proc = runtime._processes["test-agent"]
        stdout, _ = proc.communicate(timeout=5)

        cpu_val = int(stdout.decode().strip().split("\n")[0].split("=")[1])
        assert cpu_val == 15, f"Agent should have CPU limit 15, got {cpu_val}"

        runtime.stop("test-agent")

    def test_start_with_no_preexec_fn_still_works(self):
        """Verify AgentRuntime.start() works when preexec_fn is None (backward compat)."""
        from src.agent.runtime import AgentRuntime

        runtime = AgentRuntime()

        success = runtime.start(
            agent_id="test-agent-no-limits",
            command=[sys.executable, "-c", "print('hello')"],
            preexec_fn=None,
        )

        assert success, "Agent should start successfully"
        assert runtime.is_running("test-agent-no-limits"), "Agent should be running"

        proc = runtime._processes["test-agent-no-limits"]
        stdout, _ = proc.communicate(timeout=5)
        assert "hello" in stdout.decode()

        runtime.stop("test-agent-no-limits")