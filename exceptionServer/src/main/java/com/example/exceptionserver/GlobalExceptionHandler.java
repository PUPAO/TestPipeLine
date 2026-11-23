@RestControllerAdvice
@RequiredArgsConstructor
public class GlobalExceptionHandler {

	private final IncidentAlertClient incidentAlertClient;

	@ExceptionHandler(Exception.class)
	public ResponseEntity> handleUnhandledException(
		 Exception ex, HttpServletRequest request) {
		// 비동기로 Discord 알림 및 n8n 복구 파이프라인 트리거
		incidentAlertClient.reportAsync(ex, request);

		return ResponseEntity.internalServerError()
			 .body(Map.of("code", "INTERNAL_SERVER_ERROR", "message", "서버 내부 오류가 발생했습니다."));
	}
}