package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestHarnessKindsAndCapabilities(t *testing.T) {
	for _, item := range []struct{ name, kind string }{{"Pi", "pi"}, {"DeepSeek Harness", "deepseek"}, {"dsh", "deepseek"}} {
		agent := AgentConfig{Name: item.name}
		if managedAgentKindForAgent(item.name) != item.kind || runtimeSessionSourceTypeForAgent(item.name) != item.kind {
			t.Fatalf("wrong identity for %s", item.name)
		}
		if !supportsManagedGateway(agent) || !supportsAgentControlChannel(agent) || !isApprovalHookSupportedAgent(agent) || !supportsManagedLiveValidation(agent) {
			t.Fatalf("missing capability: %s", item.name)
		}
	}
}

func TestHarnessPatchPreservesUserNodes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cordis.patch.yml")
	original := "# user comment\n- id: custom\n  config:\n    enabled: true\n"
	if err := os.WriteFile(path, []byte(original), 0600); err != nil {
		t.Fatal(err)
	}
	rows := []interface{}{map[string]interface{}{"id": "preloop-control", "name": "plugin.mjs"}}
	for i := 0; i < 2; i++ {
		if err := updateHarnessPatch(path, rows); err != nil {
			t.Fatal(err)
		}
	}
	data, _ := os.ReadFile(path)
	if strings.Count(string(data), "id: preloop-control") != 1 || !strings.Contains(string(data), "user comment") {
		t.Fatalf("patch lost user config or duplicated entries: %s", data)
	}
	if err := updateHarnessPatch(path, nil); err != nil {
		t.Fatal(err)
	}
	data, _ = os.ReadFile(path)
	if strings.Contains(string(data), "preloop-control") || !strings.Contains(string(data), "id: custom") {
		t.Fatalf("bad offboard: %s", data)
	}
}

func TestHarnessGatewayAndApprovalPreservation(t *testing.T) {
	agent := AgentConfig{Name: "Pi", ConfigPath: filepath.Join(t.TempDir(), "preloop.json")}
	plan := managedMCPEnrollmentPlan{Agent: agent, ManagedDocument: map[string]interface{}{}}
	plan, err := applyHarnessManagedGateway(plan, "https://example.com", "test-token", "provider/model", []string{"provider/model", "provider/other"})
	if err != nil {
		t.Fatal(err)
	}
	spec, err := buildHarnessLiveValidationSpec(liveValidationContext{Document: plan.ManagedDocument, Prompt: "probe"})
	if err != nil || spec.Token != "test-token" || spec.ModelAlias != "provider/model" {
		t.Fatalf("invalid model probe: %+v %v", spec, err)
	}
	control := map[string]interface{}{"native_tool_approvals": "on", "approval_timeout_ms": 120000}
	ensureObjectPath(plan.ManagedDocument, "preloop")["control"] = control
	applyAgentControlConfigToDocument(agent, plan.ManagedDocument, map[string]interface{}{"bearer_token": "rotated"})
	saved, _ := agentControlConfigFromDocument(agent, plan.ManagedDocument)
	if saved["native_tool_approvals"] != "on" || saved["bearer_token"] != "rotated" {
		t.Fatalf("approval config lost: %+v", saved)
	}
}

func TestPiNativeModelImport(t *testing.T) {
	root := t.TempDir()
	for name, body := range map[string]string{"settings.json": `{"defaultProvider":"custom","defaultModel":"test-model"}`, "models.json": `{"providers":{"custom":{"baseUrl":"https://example.com/v1","apiKey":"test-key"}}}`} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
	result, err := parseHarnessManagedGatewayUpstream(AgentConfig{Name: "Pi", ConfigPath: filepath.Join(root, "preloop.json")})
	if err != nil || result == nil || !result.CanRouteThroughGateway() || result.ModelIdentifier != "test-model" || result.APIKey != "test-key" {
		t.Fatalf("bad model import: %+v %v", result, err)
	}
}

func TestHarnessDiscoveryAndOffboarding(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	for _, name := range []string{"Pi", "DeepSeek Harness"} {
		var spec agentSpec
		for _, candidate := range agentSpecs {
			if candidate.Name == name {
				spec = candidate
				break
			}
		}
		marker := filepath.Join(home, spec.DetectionPaths[0])
		if err := os.MkdirAll(filepath.Dir(marker), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(marker, []byte("{}"), 0600); err != nil {
			t.Fatal(err)
		}
		path, ok := detectInstalledAgent(home, spec)
		if !ok {
			t.Fatalf("%s was not discovered", name)
		}
		agent := AgentConfig{Name: name, ConfigPath: path}
		if !allowsSynthesizedEmptyConfig(agent) {
			t.Fatal("cannot bootstrap config")
		}
		if err := writeJSONDocument(path, map[string]interface{}{}); err != nil {
			t.Fatal(err)
		}
		if err := registerHarnessPlugin(agent); err != nil {
			t.Fatal(err)
		}
		if err := removeHarnessPlugin(agent); err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(marker); err != nil {
			t.Fatal("native settings removed")
		}
	}
}

func TestHarnessInstallRuntimeSpecs(t *testing.T) {
	for _, kind := range []string{"pi", "deepseek", "dsh"} {
		spec, err := runtimeInstallSpecForKind(kind)
		if err != nil || !strings.Contains(spec.installSummary, "--ignore-scripts") || !isExtensionHarness(AgentConfig{Name: spec.onboardAgentName}) {
			t.Fatalf("invalid runtime install: %+v %v", spec, err)
		}
	}
}

func TestHarnessHomeOverrides(t *testing.T) {
	home := t.TempDir()
	custom := t.TempDir()
	t.Setenv("DSH_HOME", custom)
	if got := expandAgentConfigPath(home, filepath.Join(home, ".dsh", "preloop.json")); got != filepath.Join(custom, "preloop.json") {
		t.Fatalf("wrong custom home: %s", got)
	}
}

func TestPiRegistrationPreservesUnownedExtension(t *testing.T) {
	agent := AgentConfig{Name: "Pi", ConfigPath: filepath.Join(t.TempDir(), "preloop.json")}
	path := filepath.Join(filepath.Dir(agent.ConfigPath), "extensions", "preloop", "index.ts")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	original := []byte("export default function custom() {}\n")
	if err := os.WriteFile(path, original, 0600); err != nil {
		t.Fatal(err)
	}
	if err := registerHarnessPlugin(agent); err == nil {
		t.Fatal("overwrote user extension")
	}
	actual, _ := os.ReadFile(path)
	if string(actual) != string(original) {
		t.Fatal("user extension changed")
	}
}

func TestHarnessValidationChecksConfiguredGateway(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	agent := AgentConfig{Name: "Pi", ConfigPath: filepath.Join(t.TempDir(), "preloop.json")}
	entry := harnessPluginEntry(agent)
	if err := os.MkdirAll(filepath.Dir(entry), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(entry, []byte("export default () => {}"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := registerHarnessPlugin(agent); err != nil {
		t.Fatal(err)
	}
	base := "https://example.com"
	doc := map[string]interface{}{
		"mcpServers": map[string]interface{}{"preloop": map[string]interface{}{"url": base + "/mcp/v1", "transport": "http", "headers": map[string]interface{}{"Authorization": "Bearer test"}}},
		"preloop":    map[string]interface{}{"control": buildManagedAgentControlConfig(agent, base, "test", nil, nil, nil)},
	}
	adapter := harnessManagedAdapter{genericManagedMCPAdapter{agent: agent}}
	if result := adapter.ValidateManagedConfig(doc, base); result["validation_passed"] != true {
		t.Fatalf("MCP-only failed: %+v", result)
	}
	plan, err := applyHarnessManagedGateway(managedMCPEnrollmentPlan{Agent: agent, ManagedDocument: doc}, base, "test", "provider/model", nil)
	if err != nil {
		t.Fatal(err)
	}
	doc = plan.ManagedDocument
	if result := adapter.ValidateManagedConfig(doc, base); result["validation_passed"] != true {
		t.Fatalf("gateway failed: %+v", result)
	}
	model, _ := asObjectMap(ensureObjectPath(doc, "preloop")["model"])
	for _, key := range []string{"baseUrl", "apiKey", "api", "models"} {
		original := model[key]
		model[key] = nil
		if adapter.ValidateManagedConfig(doc, base)["validation_passed"] != false {
			t.Fatalf("accepted invalid %s", key)
		}
		model[key] = original
	}
}

func TestHarnessRefreshRejectsEmptyCatalogWithoutRemovingGateway(t *testing.T) {
	model := map[string]interface{}{"models": []interface{}{map[string]interface{}{"id": "provider/model"}}}
	doc := map[string]interface{}{"preloop": map[string]interface{}{"model": model}}
	_, err := refreshHarnessModelDocument(AgentConfig{Name: "Pi"}, doc, nil, nil)
	if err == nil {
		t.Fatal("empty authorized catalog was accepted")
	}
	if len(asArrayValue(model["models"])) != 1 {
		t.Fatal("existing provider was removed")
	}
}
