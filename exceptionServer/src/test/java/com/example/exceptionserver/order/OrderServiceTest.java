package com.example.exceptionserver.order;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class OrderServiceTest {

    private final OrderService orderService = new OrderService();

    @Test
    @DisplayName("WELCOME10 쿠폰 적용 시 10% 할인가를 반환한다")
    void calculateDiscountPrice_withValidCoupon() {
        int result = orderService.calculateDiscountPrice(10000, "WELCOME10");
        assertEquals(9000, result);
    }

    @Test
    @DisplayName("앞뒤 공백과 소문자가 섞인 welcome10 쿠폰도 할인을 적용한다")
    void calculateDiscountPrice_withTrimmedLowerCaseCoupon() {
        int result = orderService.calculateDiscountPrice(10000, "  welcome10  ");
        assertEquals(9000, result);
    }

    @Test
    @DisplayName("couponCode가 null이면 NPE 없이 정가를 반환한다")
    void calculateDiscountPrice_withNullCoupon() {
        int result = orderService.calculateDiscountPrice(10000, null);
        assertEquals(10000, result);
    }

    @Test
    @DisplayName("유효하지 않은 쿠폰 코드는 정가를 반환한다")
    void calculateDiscountPrice_withInvalidCoupon() {
        int result = orderService.calculateDiscountPrice(10000, "INVALID");
        assertEquals(10000, result);
    }
}
