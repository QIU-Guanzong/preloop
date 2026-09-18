package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

func codexManagedOAuthSiblingForTest(id, identifier, alias, secretID string) aiModelResponse {
	return aiModelResponse{
		ID:                  id,
		Name:                "Codex CLI " + alias,
		ProviderName:        "openai-codex",
		ModelIdentifier:     identifier,
		APIEndpoint:         "https://api.openai.com/v1",
		CredentialType:      "oauth_openai_codex",
		CredentialsSecretID: secretID,
		HasAPIKey:           secretID != "",
		MetaData: map[string]interface{}{
			"source_agent":     "codex",
			"managed_by":       "preloop agents onboard",
			"managed_agent_id": "agent-codex-1",
			"gateway": map[string]interface{}{
				"enabled":     true,
				"model_alias": alias,
			},
		},
	}
}

func codexUpstreamForTest(identifier, alias string, payload map[string]interface{}) *managedGatewayUpstream {
	upstream := &managedGatewayUpstream{
		SourceAgent:       "codex",
		SourceProviderID:  "openai-codex",
		ProviderName:      "openai-codex",
		ModelIdentifier:   identifier,
		ManagedModelAlias: alias,
		APIEndpoint:       "https://api.openai.com/v1",
	}
	if payload != nil {
		upstream.CredentialType = "oauth_openai_codex"
		upstream.CredentialPayload = payload
	}
	return upstream
}

func newCodexFamilyLineageServer(
	t *testing.T,
	models []aiModelResponse,
	writes *[]recordedAIModelWrite,
) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode(models)
		case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/api/v1/ai-models/"):
			body := map[string]interface{}{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("failed to decode ai-model update: %v", err)
			}
			*writes = append(*writes, recordedAIModelWrite{
				Method: http.MethodPut,
				Path:   r.URL.Path,
				Body:   body,
			})
			updated := models[0]
			for _, m := range models {
				if strings.HasSuffix(r.URL.Path, "/"+m.ID) {
					updated = m
					break
				}
			}
			if secretID, ok := body["credentials_secret_id"].(string); ok {
				updated.CredentialsSecretID = secretID
				updated.HasAPIKey = true
			}
			_ = json.NewEncoder(w).Encode(updated)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/ai-models":
			body := map[string]interface{}{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("failed to decode ai-model create: %v", err)
			}
			*writes = append(*writes, recordedAIModelWrite{
				Method: http.MethodPost,
				Path:   r.URL.Path,
				Body:   body,
			})
			created := codexManagedOAuthSiblingForTest(
				"created-model",
				"gpt-4o",
				"openai/gpt-4o",
				"",
			)
			if secretID, ok := body["credentials_secret_id"].(string); ok {
				created.CredentialsSecretID = secretID
				created.HasAPIKey = true
			}
			_ = json.NewEncoder(w).Encode(created)
		default:
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

func TestSyncManagedGatewayAIModelCreateReusesCodexSiblingSecret(t *testing.T) {
	sibling := codexManagedOAuthSiblingForTest(
		"codex-o3-mini",
		"o3-mini",
		"openai/o3-mini",
		"secret-codex-live",
	)
	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{sibling}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	model, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model == nil {
		t.Fatalf("expected created codex row, got nil")
	}
	var createBody map[string]interface{}
	for _, write := range writes {
		if write.Method == http.MethodPut {
			t.Fatalf("must not update on create; got %#v", write)
		}
		if write.Method == http.MethodPost {
			createBody = write.Body
		}
	}
	if createBody == nil {
		t.Fatalf("expected a create for the new codex row; writes: %#v", writes)
	}
	if createBody["credentials_secret_id"] != "secret-codex-live" {
		t.Fatalf("expected create to reuse sibling secret, got %#v", createBody)
	}
	if _, ok := createBody["credential_payload"]; ok {
		t.Fatalf("create must not mint a second OAuth secret; got %#v", createBody)
	}
	if _, ok := createBody["credential_type"]; ok {
		t.Fatalf("create must not send credential_type with a reused secret; got %#v", createBody)
	}
}

func TestSyncManagedGatewayAIModelUpdateAttachesCodexSiblingSecret(t *testing.T) {
	target := codexManagedOAuthSiblingForTest(
		"target-codex-gpt4",
		"gpt-4o",
		"openai/gpt-4o",
		"separate-secret-id",
	)
	sibling := codexManagedOAuthSiblingForTest(
		"sibling-codex-o3",
		"o3-mini",
		"openai/o3-mini",
		"shared-codex-secret",
	)
	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{target, sibling}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	model, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if model == nil {
		t.Fatalf("expected updated codex row, got nil")
	}
	repointed := false
	for _, write := range writes {
		if write.Method == http.MethodPut && strings.HasSuffix(write.Path, "/target-codex-gpt4") {
			if write.Body["credentials_secret_id"] == "shared-codex-secret" {
				repointed = true
			}
		}
	}
	if !repointed {
		t.Fatalf("expected PUT repointing target to shared-codex-secret; writes: %#v", writes)
	}
}

func TestSyncManagedGatewayAIModelDifferentManagedAgentDoesNotAttachCodex(t *testing.T) {
	otherMachine := codexManagedOAuthSiblingForTest(
		"codex-other",
		"o3-mini",
		"openai/o3-mini",
		"secret-other-machine",
	)
	otherMachine.MetaData["managed_agent_id"] = "agent-other-machine"

	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{otherMachine}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	_, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, write := range writes {
		if write.Method == http.MethodPost {
			if write.Body["credentials_secret_id"] == "secret-other-machine" {
				t.Fatalf("must never attach to another machine's managed_agent_id secret: %#v", write)
			}
		}
	}
}

func TestFindManagedOAuthCredentialSiblingPrefersFresherOverFirstMatch(t *testing.T) {
	staleFirst := codexManagedOAuthSiblingForTest(
		"codex-stale-first",
		"o3-mini",
		"openai/o3-mini",
		"secret-stale",
	)
	staleFirst.UpdatedAt = "2026-09-01T00:00:00Z"
	staleFirst.CredentialsLastVerifiedAt = "2026-09-01T00:00:00Z"

	freshLater := codexManagedOAuthSiblingForTest(
		"codex-fresh-later",
		"gpt-4o",
		"openai/gpt-4o",
		"secret-live",
	)
	freshLater.UpdatedAt = "2026-09-18T00:00:00Z"
	freshLater.CredentialsLastVerifiedAt = "2026-09-18T12:00:00Z"

	got := findManagedOAuthCredentialSibling(
		[]aiModelResponse{staleFirst, freshLater},
		&managedAgentSummary{ID: "agent-codex-1"},
		"oauth_openai_codex",
		"",
	)
	if got == nil || got.ID != "codex-fresh-later" {
		t.Fatalf("stale first-listed sibling must lose to the fresher copy, got %#v", got)
	}
	if got.CredentialsSecretID != "secret-live" {
		t.Fatalf("expected live secret, got %#v", got)
	}
}

func TestSyncManagedGatewayAIModelCreateReusesFresherCodexSiblingSecret(t *testing.T) {
	staleFirst := codexManagedOAuthSiblingForTest(
		"codex-stale-first",
		"o3-mini",
		"openai/o3-mini",
		"secret-stale",
	)
	staleFirst.CredentialsLastVerifiedAt = "2026-09-01T00:00:00Z"
	staleFirst.UpdatedAt = "2026-09-01T00:00:00Z"

	freshLater := codexManagedOAuthSiblingForTest(
		"codex-fresh-later",
		"gpt-5",
		"openai/gpt-5",
		"secret-live",
	)
	freshLater.CredentialsLastVerifiedAt = "2026-09-18T12:00:00Z"
	freshLater.UpdatedAt = "2026-09-18T12:00:00Z"

	writes := []recordedAIModelWrite{}
	server := newCodexFamilyLineageServer(t, []aiModelResponse{staleFirst, freshLater}, &writes)
	defer server.Close()

	freshExpiry := time.Now().UTC().Add(4 * time.Hour).UnixMilli()
	upstream := codexUpstreamForTest("gpt-4o", "openai/gpt-4o", map[string]interface{}{
		"access":  "sk-codex-oat-fresh",
		"refresh": "sk-codex-ort-fresh",
		"expires": freshExpiry,
	})

	_, _, err := syncManagedGatewayAIModel(
		api.NewClientWithToken(server.URL, "tok"),
		&managedAgentSummary{ID: "agent-codex-1"},
		AgentConfig{Name: "Codex CLI"},
		upstream,
		server.URL+"/openai/v1",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var createBody map[string]interface{}
	for _, write := range writes {
		if write.Method == http.MethodPost {
			createBody = write.Body
		}
	}
	if createBody == nil {
		t.Fatalf("expected a create for the new codex row; writes: %#v", writes)
	}
	if createBody["credentials_secret_id"] != "secret-live" {
		t.Fatalf("create must reuse the fresher sibling secret, got %#v", createBody)
	}
}

func TestCodexServerHasReusableGatewayCredential(t *testing.T) {
	sibling := codexManagedOAuthSiblingForTest(
		"codex-o3-mini",
		"o3-mini",
		"openai/o3-mini",
		"secret-codex-live",
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models" {
			_ = json.NewEncoder(w).Encode([]aiModelResponse{sibling})
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	agent := AgentConfig{Name: "Codex CLI"}
	upstream := codexUpstreamForTest("o3-mini", "openai/o3-mini", nil)

	if !serverHasReusableGatewayCredential(client, agent, upstream) {
		t.Fatalf("expected serverHasReusableGatewayCredential to be true for Codex CLI with stored model")
	}
}
