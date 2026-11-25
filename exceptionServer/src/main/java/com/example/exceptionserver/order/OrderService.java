package com.example.exceptionserver.order;

import org.springframework.stereotype.Service;

@Service
public class OrderService {

	// 의도적 버그: couponCode가 null로 들어오면 NullPointerException 발생
	public int calculateDiscountPrice(int originalPrice, String couponCode) {
		if (couponCode != null && couponCode.trim().equalsIgnoreCase("WELCOME10")) {
			return (int) (originalPrice * 0.9);
		}
		return originalPrice;
	}
}