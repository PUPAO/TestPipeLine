package com.example.global.error;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletRequest;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.io.PrintWriter;
import java.io.StringWriter;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

@Slf4j
@Component
@RequiredArgsConstructor
public class IncidentAlertClient {

	private final ObjectMapper objectMapper;
	private final RestClient restClient = RestClient.create();

	@Value("${incident.alert.enabled:false}")
	private boolean enabled;

	@Value("${incident.alert.service-name:spring-backend}")
	private String serviceName;

	@Value("${incident.alert.environment:local}")
	private String environment;

	@Value("${incident.alert.discord-webhook-url:}")
	private String discordWebhookUrl;

	@Value("${incident.alert.n8n-webhook-url:}")
	private String n8nWebhookUrl;

	public void reportAsync(Exception ex, HttpServletRequest request) {
		if (!enabled) {
			return;
		}

		String exceptionClass = ex.getClass().getName();
		String message = ex.getMessage() != null ? ex.getMessage() : "No message";
		String stackTrace = extractStackTrace(ex);
		String method = request != null ? request.getMethod() : "UNKNOWN";
		String uri = request != null ? request.getRequestURI() : "UNKNOWN";
		String occurredAt = OffsetDateTime.now(ZoneId.of("Asia/Seoul")).toString();

		CompletableFuture.runAsync(() -> {
			sendToDiscord(exceptionClass, message, stackTrace, method, uri, occurredAt);
			sendToN8n(exceptionClass, message, stackTrace, method, uri, occurredAt);
		});
	}

	private void sendToDiscord(String exceptionClass, String message, String stackTrace,
		 String method, String uri, String occurredAt) {
		if (discordWebhookUrl == null || discordWebhookUrl.isBlank()) {
			return;
		}
		try {
			Map<String, Object> embed = Map.of(
				 "title", "🚨 [" + serviceName + " / " + environment + "] Unhandled Exception",
				 "color", 15158332,
				 "fields", List.of(
					  Map.of("name", "Endpoint", "value", "`" + method + " " + uri + "`", "inline", true),
					  Map.of("name", "Exception", "value", "`" + truncate(exceptionClass, 200) + "`", "inline", true),
					  Map.of("name", "Message", "value", truncate(message, 500), "inline", false),
					  Map.of("name", "Stack Trace (Top)", "value", "```java\n" + truncate(stackTrace, 950) + "\n```", "inline", false)
				 ),
				 "footer", Map.of("text", "Occurred at " + occurredAt)
			);

			String payload = objectMapper.writeValueAsString(Map.of("embeds", List.of(embed)));
			restClient.post()
				 .uri(discordWebhookUrl)
				 .contentType(MediaType.APPLICATION_JSON)
				 .body(payload)
				 .retrieve()
				 .toBodilessEntity();
		} catch (Exception e) {
			log.warn("Failed to send incident alert to Discord: {}", e.getMessage());
		}
	}

	private void sendToN8n(String exceptionClass, String message, String stackTrace,
		 String method, String uri, String occurredAt) {
		if (n8nWebhookUrl == null || n8nWebhookUrl.isBlank()) {
			return;
		}
		try {
			Map<String, Object> body = Map.of(
				 "serviceName", serviceName,
				 "environment", environment,
				 "exceptionClass", exceptionClass,
				 "message", message,
				 "stackTrace", truncate(stackTrace, 6000),
				 "httpMethod", method,
				 "requestUri", uri,
				 "occurredAt", occurredAt
			);

			String payload = objectMapper.writeValueAsString(body);
			restClient.post()
				 .uri(n8nWebhookUrl)
				 .contentType(MediaType.APPLICATION_JSON)
				 .body(payload)
				 .retrieve()
				 .toBodilessEntity();
		} catch (Exception e) {
			log.warn("Failed to trigger n8n incident pipeline: {}", e.getMessage());
		}
	}

	private String extractStackTrace(Throwable throwable) {
		StringWriter sw = new StringWriter();
		throwable.printStackTrace(new PrintWriter(sw));
		return sw.toString();
	}

	private String truncate(String text, int maxLength) {
		if (text == null) return "";
		return text.length() <= maxLength ? text : text.substring(0, maxLength) + "...";
	}
}