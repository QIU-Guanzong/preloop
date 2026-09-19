package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

const harnessPluginPackage = "@preloop-ai/harness-plugin"
const harnessPluginSpec = harnessPluginPackage + "@0.1.0"
const harnessPatchMarker = "Managed by Preloop: Pi / DeepSeek Harness"

func isExtensionHarness(agent AgentConfig) bool {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "pi", "deepseek", "deepseek harness", "dsh":
		return true
	}
	return false
}

func harnessPluginRoot() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".preloop", "runtime-plugins", "harness")
}

func harnessPluginEntry(agent AgentConfig) string {
	return filepath.Join(harnessPluginRoot(), "node_modules", "@preloop-ai", "harness-plugin", runtimeSessionSourceTypeForAgent(agent.Name)+".mjs")
}

func applyHarnessManagedGateway(plan managedMCPEnrollmentPlan, baseURL, token, modelAlias string, aliases []string) (managedMCPEnrollmentPlan, error) {
	models := []interface{}{map[string]interface{}{"id": modelAlias}}
	for _, alias := range aliases {
		if alias != modelAlias {
			models = append(models, map[string]interface{}{"id": alias})
		}
	}
	ensureObjectPath(plan.ManagedDocument, "preloop")["model"] = map[string]interface{}{
		"api": "openai-completions", "baseUrl": strings.TrimRight(baseURL, "/") + openClawGatewayPath,
		"apiKey": token, "models": models,
	}
	return refreshManagedPlanSnapshots(plan)
}

type harnessManagedAdapter struct{ genericManagedMCPAdapter }

func (a harnessManagedAdapter) ValidateManagedConfig(doc map[string]interface{}, baseURL string) map[string]interface{} {
	result := a.genericManagedMCPAdapter.ValidateManagedConfig(doc, baseURL)
	rawModel, configured := ensureObjectPath(doc, "preloop")["model"]
	model, _ := asObjectMap(rawModel)
	result["gateway_provider_ok"] = model != nil
	result["gateway_base_url_ok"] = lookupString(model, "baseUrl") == strings.TrimRight(baseURL, "/")+openClawGatewayPath
	result["gateway_token_ok"] = lookupString(model, "apiKey") != ""
	result["model_provider_rewritten"] = model != nil
	result["gateway_models_ok"] = false
	if models := asArrayValue(model["models"]); len(models) > 0 {
		if first, ok := asObjectMap(models[0]); ok {
			result["gateway_model_alias"] = lookupString(first, "id")
			result["gateway_models_ok"] = strings.TrimSpace(lookupString(first, "id")) != ""
		}
	}
	for key, value := range validateAgentControlConfig(a.agent, doc, baseURL) {
		result[key] = value
	}
	gatewayOK := !configured || (result["gateway_provider_ok"] == true &&
		result["gateway_base_url_ok"] == true && result["gateway_token_ok"] == true &&
		result["gateway_models_ok"] == true && lookupString(model, "api") == "openai-completions")
	result["validation_passed"] = result["validation_passed"] == true && result["control_channel_configured"] == true && gatewayOK
	return result
}

func installHarnessPlugin(agent AgentConfig, out io.Writer) map[string]interface{} {
	result := map[string]interface{}{"control_plugin_installed": false, "control_plugin_verified": false}
	target := harnessPluginSpec
	if local, ok := findAgentControlRuntimePluginSource(agent); ok {
		target = local
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	command := exec.CommandContext(ctx, "npm", "install", "--ignore-scripts", "--install-links", "--no-audit", "--no-fund", "--prefix", harnessPluginRoot(), target)
	if output, err := command.CombinedOutput(); err != nil {
		result["control_plugin_install_status"] = "failed"
		if out != nil {
			fmt.Fprintf(out, "Preloop harness plugin installation failed: %s\n", strings.TrimSpace(string(output)))
		}
		return result
	}
	if err := registerHarnessPlugin(agent); err != nil {
		result["control_plugin_install_status"] = "registration_failed"
		if out != nil {
			fmt.Fprintf(out, "Preloop harness registration failed: %v\n", err)
		}
		return result
	}
	return verifyHarnessPlugin(agent)
}

func verifyHarnessPlugin(agent AgentConfig) map[string]interface{} {
	_, entryErr := os.Stat(harnessPluginEntry(agent))
	registered := false
	if runtimeSessionSourceTypeForAgent(agent.Name) == "pi" {
		data, err := os.ReadFile(filepath.Join(filepath.Dir(agent.ConfigPath), "extensions", "preloop", "index.ts"))
		registered = err == nil && strings.Contains(string(data), harnessPatchMarker)
	} else {
		data, err := os.ReadFile(filepath.Join(filepath.Dir(agent.ConfigPath), "cordis.patch.yml"))
		registered = err == nil && strings.Contains(string(data), harnessPatchMarker)
	}
	return map[string]interface{}{"control_plugin_installed": entryErr == nil, "control_plugin_verified": entryErr == nil && registered,
		"control_plugin_verification": "local_registration_checked_restart_required"}
}

func registerHarnessPlugin(agent AgentConfig) error {
	if runtimeSessionSourceTypeForAgent(agent.Name) == "pi" {
		path := filepath.Join(filepath.Dir(agent.ConfigPath), "extensions", "preloop", "index.ts")
		if data, err := os.ReadFile(path); err == nil {
			if !strings.HasPrefix(string(data), "// "+harnessPatchMarker+"\n") {
				return fmt.Errorf("refusing to overwrite an existing Pi extension at %s", path)
			}
		} else if !os.IsNotExist(err) {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			return err
		}
		entry, _ := json.Marshal((&url.URL{Scheme: "file", Path: harnessPluginEntry(agent)}).String())
		return os.WriteFile(path, []byte("// "+harnessPatchMarker+"\nexport { default } from "+string(entry)+";\n"), 0600)
	}
	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		return err
	}
	rows := []interface{}{}
	if model, ok := asObjectMap(ensureObjectPath(doc, "preloop")["model"]); ok {
		models := asArrayValue(model["models"])
		if len(models) > 0 {
			first, _ := asObjectMap(models[0])
			// The plugin loads its token before requests run; no credential is duplicated in the patch.
			rows = append(rows, map[string]interface{}{"id": "llm-pi-ai", "config": map[string]interface{}{"providers": map[string]interface{}{"preloop": map[string]interface{}{
				"api": model["api"], "baseURL": model["baseUrl"], "apiKeyEnv": "PRELOOP_DSH_MODEL_TOKEN", "models": models,
			}}}}, map[string]interface{}{"id": "agent-default-model", "config": map[string]interface{}{"provider": "preloop", "model": first["id"]}})
		}
	}
	inserts := []interface{}{map[string]interface{}{"id": "preloop-control", "name": harnessPluginEntry(agent), "config": map[string]interface{}{"configPath": agent.ConfigPath}}}
	servers, _ := asObjectMap(doc["mcpServers"])
	for name, raw := range servers {
		server, ok := asObjectMap(raw)
		if !ok {
			continue
		}
		inserts = append(inserts, map[string]interface{}{"id": "preloop-mcp-" + name, "name": "@deepseek-ai/dsh-mcp-client", "config": map[string]interface{}{
			"serverName": name, "transport": "streamable-http", "url": server["url"], "headers": server["headers"], "toolCallTimeoutMs": 600000, "failOnStartupError": true,
		}})
	}
	rows = append(rows, map[string]interface{}{"insert": inserts})
	return updateHarnessPatch(filepath.Join(filepath.Dir(agent.ConfigPath), "cordis.patch.yml"), rows)
}

// Keep user-owned patch nodes and their comments; replace only our marked rows.
func updateHarnessPatch(path string, rows []interface{}) error {
	root := &yaml.Node{Kind: yaml.DocumentNode, Content: []*yaml.Node{{Kind: yaml.SequenceNode, Tag: "!!seq"}}}
	data, err := os.ReadFile(path)
	if err == nil && len(strings.TrimSpace(string(data))) > 0 {
		if err := yaml.Unmarshal(data, root); err != nil {
			return err
		}
	} else if err != nil && !os.IsNotExist(err) {
		return err
	}
	if len(root.Content) != 1 || root.Content[0].Kind != yaml.SequenceNode {
		return fmt.Errorf("DeepSeek global patch must be a YAML sequence")
	}
	seq := root.Content[0]
	kept := []*yaml.Node{}
	for _, node := range seq.Content {
		if !strings.Contains(node.HeadComment, harnessPatchMarker) {
			kept = append(kept, node)
		}
	}
	for _, row := range rows {
		node := &yaml.Node{}
		if err := node.Encode(row); err != nil {
			return err
		}
		node.HeadComment = harnessPatchMarker
		kept = append(kept, node)
	}
	seq.Content = kept
	encoded, err := yaml.Marshal(root)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	if err := os.WriteFile(path, encoded, 0600); err != nil {
		return err
	}
	return os.Chmod(path, 0600)
}

func removeHarnessPlugin(agent AgentConfig) error {
	if runtimeSessionSourceTypeForAgent(agent.Name) == "pi" {
		path := filepath.Join(filepath.Dir(agent.ConfigPath), "extensions", "preloop", "index.ts")
		data, err := os.ReadFile(path)
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		if strings.Contains(string(data), harnessPatchMarker) {
			return os.Remove(path)
		}
		return nil
	}
	path := filepath.Join(filepath.Dir(agent.ConfigPath), "cordis.patch.yml")
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return nil
	}
	return updateHarnessPatch(path, nil)
}

func installHarnessApprovals(agent AgentConfig, baseURL, token string) error {
	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		return err
	}
	control, ok := agentControlConfigFromDocument(agent, doc)
	if !ok {
		control = buildManagedAgentControlConfig(agent, baseURL, token, nil, nil, nil)
	}
	control["bearer_token"] = token
	control["native_tool_approvals"] = "on"
	control["approval_timeout_ms"] = resolveApprovalHookTimeoutSeconds() * 1000
	ensureObjectPath(doc, "preloop")["control"] = control
	return writeJSONDocument(agent.ConfigPath, doc)
}

// Inspect native provider selection without evaluating Pi's executable secret expressions.
func parseHarnessManagedGatewayUpstream(agent AgentConfig) (*managedGatewayUpstream, error) {
	root := filepath.Dir(agent.ConfigPath)
	provider, model, endpoint, key := "", "", "", ""
	if runtimeSessionSourceTypeForAgent(agent.Name) == "pi" {
		settings, _, err := loadJSONDocumentIfExists(filepath.Join(root, "settings.json"))
		if err != nil {
			return nil, err
		}
		provider = lookupString(settings, "defaultProvider")
		model = lookupString(settings, "defaultModel")
		models, _, err := loadJSONDocumentIfExists(filepath.Join(root, "models.json"))
		if err != nil {
			return nil, err
		}
		custom, _ := asObjectMap(ensureObjectPath(models, "providers")[provider])
		endpoint = lookupString(custom, "baseUrl")
		raw := lookupString(custom, "apiKey")
		if !strings.HasPrefix(raw, "!") {
			key = resolveConfigSecret(raw)
		}
		auth, _, err := loadJSONDocumentIfExists(filepath.Join(root, "auth.json"))
		if err != nil {
			return nil, err
		}
		entry, _ := asObjectMap(auth[provider])
		if key == "" && lookupString(entry, "type") == "api_key" {
			key = lookupString(entry, "key")
		}
	} else {
		data, err := os.ReadFile(filepath.Join(root, "settings.yaml"))
		if err != nil && !os.IsNotExist(err) {
			return nil, err
		}
		settings := map[string]interface{}{}
		if len(data) > 0 {
			settings, err = decodeHermesYAMLDocument(data)
			if err != nil {
				return nil, err
			}
		}
		selection, _ := asObjectMap(settings["agent-default-model"])
		provider = lookupString(selection, "provider")
		model = lookupString(selection, "model")
		if provider == "" {
			provider = "deepseek"
		}
		if model == "" {
			model = "deepseek-flash"
		}
		route, _ := asObjectMap(ensureObjectPath(settings, "llm-pi-ai", "providers")[provider])
		endpoint = lookupString(route, "baseURL")
		envKey := lookupString(route, "apiKeyEnv")
		if envKey != "" {
			key = os.Getenv(envKey)
			if key == "" {
				key = resolveEnvFileSecret(filepath.Join(root, ".env"), envKey)
			}
		}
		if provider == "deepseek-official" {
			provider = "deepseek"
		}
	}
	if provider == "" || model == "" || provider == "preloop" {
		return nil, nil
	}
	if key == "" {
		envKey := strings.ToUpper(strings.ReplaceAll(provider, "-", "_")) + "_API_KEY"
		key = os.Getenv(envKey)
		if key == "" {
			key = resolveEnvFileSecret(filepath.Join(root, ".env"), envKey)
		}
	}
	if endpoint == "" {
		endpoint = map[string]string{"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1", "deepseek": "https://api.deepseek.com", "openrouter": "https://openrouter.ai/api/v1"}[provider]
	}
	return &managedGatewayUpstream{SourceAgent: runtimeSessionSourceTypeForAgent(agent.Name), SourceProviderID: provider, ProviderName: provider,
		ModelIdentifier: model, APIEndpoint: endpoint, APIKey: key, ManagedModelAlias: provider + "/" + model}, nil
}

func buildHarnessLiveValidationSpec(ctx liveValidationContext) (gatewayLiveValidationSpec, error) {
	model, _ := asObjectMap(ensureObjectPath(ctx.Document, "preloop")["model"])
	models := asArrayValue(model["models"])
	if len(models) == 0 {
		return gatewayLiveValidationSpec{}, fmt.Errorf("no managed harness model configured")
	}
	first, _ := asObjectMap(models[0])
	alias := lookupString(first, "id")
	return gatewayLiveValidationSpec{Endpoint: "/openai/v1/chat/completions", Body: buildChatCompletionsLiveValidationPayload(alias, ctx.Prompt), Token: lookupString(model, "apiKey"), ModelAlias: alias}, nil
}

func refreshHarnessModelDocument(agent AgentConfig, doc map[string]interface{}, models []aiModelResponse, bindings []managedAgentModelBindingSummary) (managedModelRefreshOutcome, error) {
	model, ok := asObjectMap(ensureObjectPath(doc, "preloop")["model"])
	if !ok {
		return managedModelRefreshOutcome{SkipReason: "no managed model provider configured"}, nil
	}
	authorized := authorizedGatewayModelAliases(models, bindings)
	if len(authorized) == 0 {
		return managedModelRefreshOutcome{}, fmt.Errorf("no authorized models for %s; grant a model before refreshing", agent.Name)
	}
	before := []string{}
	entries := map[string]interface{}{}
	for _, raw := range asArrayValue(model["models"]) {
		if entry, ok := asObjectMap(raw); ok {
			alias := lookupString(entry, "id")
			before = append(before, alias)
			entries[alias] = entry
		}
	}
	selected := ""
	if len(before) > 0 {
		for _, alias := range authorized {
			if alias == before[0] {
				selected = alias
				break
			}
		}
	}
	if selected == "" && len(authorized) > 0 {
		selected = defaultGatewayModelAlias(models, authorized)
	}
	after := []interface{}{}
	// Keep the current selection first: both plugins use it as their startup default.
	for _, alias := range append([]string{selected}, authorized...) {
		if alias == "" || (len(after) > 0 && alias == selected) {
			continue
		}
		entry := entries[alias]
		if entry == nil {
			entry = map[string]interface{}{"id": alias}
		}
		after = append(after, entry)
	}
	model["models"] = after
	return managedModelRefreshOutcome{Doc: doc, Before: before, After: authorized, Selected: selected}, nil
}
