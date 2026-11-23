package com.example.exceptionserver.order;

import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequiredArgsConstructor
public class OrderController {

	private final OrderService orderService;

	@GetMapping("/api/orders/discount")
	public int getDiscountPrice(
		 @RequestParam(defaultValue = "10000") int price,
		 @RequestParam(required = false) String coupon
	) {
		return orderService.calculateDiscountPrice(price, coupon);
	}
}