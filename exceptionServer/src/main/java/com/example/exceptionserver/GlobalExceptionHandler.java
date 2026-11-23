package com.example.exceptionserver;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.Map;

@RestControllerAdvice
public class GlobalExceptionHandler {

	private final IncidentAlertClient incidentAlertClient;

	public GlobalExceptionHandler(IncidentAlertClient incidentAlertClient) {
		this.incidentAlertClient = incidentAlertClient;
	}

	@ExceptionHandler(Exception.class)
	public ResponseEntity<Object> handleUnhandledException(
		 Exception ex, HttpServletRequest request) {
		incidentAlertClient.reportAsync(ex, request);

		return ResponseEntity.internalServerError()
			 .body(Map.of("code", "INTERNAL_SERVER_ERROR", "message", "서버 내부 오류가 발생했습니다."));
	}
}