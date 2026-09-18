package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestAgentsValidateCodexCLIModelCredentialHealth(t *testing.T) {
	tests := []struct {
		name              string
		credStatus        string
		credLastError     string
		expectErr         bool
		expectOutputParts []string
	}{
		{
			name:          "error status fails validation and prints summary",
			credStatus:    "error",
			credLastError: "openai refresh failed (status=401, code=invalid_grant)",
			expectErr:     true,
			expectOutputParts: []string{
				"validation_failed",
				"openai refresh failed (status=401, code=invalid_grant)",
			},
		},
		{
			name:       "active status passes validation",
			credStatus: "active",
			expectErr:  false,
			expectOutputParts: []string{
				"validated",
				"active",
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			home := t.TempDir()
			testenv.SetHome(t, home)

			codexDir := filepath.Join(home, ".codex")
			if err := os.MkdirAll(codexDir, 0o755); err != nil {
				t.Fatalf("failed to create codex dir: %v", err)
			}

			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch {
				case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
					_ = json.NewEncoder(w).Encode(managedAgentListResponse{
						Items: []managedAgentSummary{{
							ID:                "agent-codex-1",
							DisplayName:       "Codex CLI",
							SessionSourceType: "codex",
							SessionSourceID:   runtimePrincipalIDForAgent(AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(codexDir, "config.toml")}),
							LifecycleState:    "active",
							LatestModelAlias:  "openai/gpt-5.4",
						}},
					})
				case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents/agent-codex-1":
					_ = json.NewEncoder(w).Encode(managedAgentDetailResponse{
						Agent: managedAgentSummary{
							ID:                "agent-codex-1",
							DisplayName:       "Codex CLI",
							SessionSourceType: "codex",
							LifecycleState:    "active",
							LatestModelAlias:  "openai/gpt-5.4",
							ConfiguredModels: []managedAgentModelBindingSummary{{
								ID:              "binding-1",
								AIModelID:       "model-codex-1",
								GatewayAlias:    "openai/gpt-5.4",
								ModelIdentifier: "gpt-5.4",
								ProviderName:    "openai",
							}},
						},
					})
				case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
					models := []aiModelResponse{{
						ID:                   "model-codex-1",
						Name:                 "Codex GPT-5.4",
						ProviderName:         "openai",
						ModelIdentifier:      "gpt-5.4",
						CredentialType:       "oauth_openai_codex",
						CredentialsStatus:    tt.credStatus,
						CredentialsLastError: tt.credLastError,
						HasAPIKey:            true,
						IsDefault:            true,
					}}
					_ = json.NewEncoder(w).Encode(models)
				default:
					http.NotFound(w, r)
				}
			}))
			defer server.Close()

			configContent := fmt.Sprintf(`model = "openai/gpt-5.4"
model_provider = "preloop"

[model_providers.preloop]
base_url = "%s/openai/v1"
experimental_bearer_token = "codex-durable-token"
wire_api = "responses"

[mcp_servers.preloop]
url = "%s/mcp/v1"

[mcp_servers.preloop.http_headers]
Authorization = "Bearer codex-durable-token"
`, server.URL, server.URL)

			if err := os.WriteFile(filepath.Join(codexDir, "config.toml"), []byte(configContent), 0o644); err != nil {
				t.Fatalf("failed to write codex config: %v", err)
			}

			oldURL := FlagURL
			oldToken := FlagToken
			FlagURL = server.URL
			FlagToken = "test-token"
			defer func() {
				FlagURL = oldURL
				FlagToken = oldToken
			}()

			// Capture stdout during runAgentsValidate
			rPipe, wPipe, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			origStdout := os.Stdout
			os.Stdout = wPipe

			runErr := runAgentsValidate(agentsValidateCmd, []string{"Codex CLI"})

			wPipe.Close()
			os.Stdout = origStdout

			var buf bytes.Buffer
			_, _ = io.Copy(&buf, rPipe)
			output := buf.String()

			if tt.expectErr && runErr == nil {
				t.Fatalf("expected validation to fail, but got nil error. Output:\n%s", output)
			}
			if !tt.expectErr && runErr != nil {
				t.Fatalf("expected validation to succeed, but got error: %v. Output:\n%s", runErr, output)
			}

			for _, part := range tt.expectOutputParts {
				if !strings.Contains(output, part) {
					t.Errorf("expected output to contain %q, but got:\n%s", part, output)
				}
			}
		})
	}
}

func TestAgentsStatusCodexCLIModelCredentialHealth(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)

	codexDir := filepath.Join(home, ".codex")
	if err := os.MkdirAll(codexDir, 0o755); err != nil {
		t.Fatalf("failed to create codex dir: %v", err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "agent-codex-1",
					DisplayName:       "Codex CLI",
					SessionSourceType: "codex",
					SessionSourceID:   runtimePrincipalIDForAgent(AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(codexDir, "config.toml")}),
					LifecycleState:    "active",
					LatestModelAlias:  "openai/gpt-5.4",
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents/agent-codex-1":
			_ = json.NewEncoder(w).Encode(managedAgentDetailResponse{
				Agent: managedAgentSummary{
					ID:                "agent-codex-1",
					DisplayName:       "Codex CLI",
					SessionSourceType: "codex",
					LifecycleState:    "active",
					LatestModelAlias:  "openai/gpt-5.4",
					ConfiguredModels: []managedAgentModelBindingSummary{{
						ID:              "binding-1",
						AIModelID:       "model-codex-1",
						GatewayAlias:    "openai/gpt-5.4",
						ModelIdentifier: "gpt-5.4",
						ProviderName:    "openai",
					}},
				},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			models := []aiModelResponse{{
				ID:                   "model-codex-1",
				Name:                 "Codex GPT-5.4",
				ProviderName:         "openai",
				ModelIdentifier:      "gpt-5.4",
				CredentialType:       "oauth_openai_codex",
				CredentialsStatus:    "error",
				CredentialsLastError: "openai refresh failed (status=401, code=invalid_grant)",
				HasAPIKey:            true,
				IsDefault:            true,
			}}
			_ = json.NewEncoder(w).Encode(models)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	configContent := fmt.Sprintf(`model = "openai/gpt-5.4"
model_provider = "preloop"

[model_providers.preloop]
base_url = "%s/openai/v1"
experimental_bearer_token = "codex-durable-token"
wire_api = "responses"

[mcp_servers.preloop]
url = "%s/mcp/v1"

[mcp_servers.preloop.http_headers]
Authorization = "Bearer codex-durable-token"
`, server.URL, server.URL)

	if err := os.WriteFile(filepath.Join(codexDir, "config.toml"), []byte(configContent), 0o644); err != nil {
		t.Fatalf("failed to write codex config: %v", err)
	}

	oldURL := FlagURL
	oldToken := FlagToken
	FlagURL = server.URL
	FlagToken = "test-token"
	defer func() {
		FlagURL = oldURL
		FlagToken = oldToken
	}()

	rPipe, wPipe, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	origStdout := os.Stdout
	os.Stdout = wPipe

	runErr := runAgentsStatus(agentsStatusCmd, []string{"Codex CLI"})

	wPipe.Close()
	os.Stdout = origStdout

	if runErr != nil {
		t.Fatalf("runAgentsStatus failed: %v", runErr)
	}

	var buf bytes.Buffer
	_, _ = io.Copy(&buf, rPipe)
	output := buf.String()

	if !strings.Contains(output, "Model status: error") {
		t.Errorf("expected output to contain 'Model status: error', got:\n%s", output)
	}
	if !strings.Contains(output, "openai refresh failed (status=401, code=invalid_grant)") {
		t.Errorf("expected output to contain credential error summary, got:\n%s", output)
	}
}
