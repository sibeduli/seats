#!/usr/bin/env python3
"""
Benchmark script to test concurrent seat booking.
Simulates 10 workers trying to book seats in WLA region simultaneously.
Each worker books 1 seat.
"""

import requests
import concurrent.futures
import time
import random
import argparse

# WLA region has 80 seats (8 rows x 10 seats)
WLA_SEATS = list(range(1, 81))

def book_seat(args):
    """Book a single seat as a guest"""
    seat_num, base_url, bypass_ratelimit = args
    
    payload = {
        "name": f"Test User {seat_num}",
        "phone": f"08123456{seat_num:04d}",
        "seats": [{"region": "WLA", "number": seat_num}]
    }
    
    headers = {'Content-Type': 'application/json'}
    if bypass_ratelimit:
        headers['X-Benchmark-Bypass'] = 'true'
    
    start = time.time()
    try:
        response = requests.post(
            f"{base_url}/api/book",
            json=payload,
            headers=headers,
            timeout=30
        )
        elapsed = time.time() - start
        data = response.json()
        
        if response.status_code == 200 and data.get('success'):
            return {
                'seat': f"WLA-{seat_num}",
                'status': 'SUCCESS',
                'time': elapsed,
                'ticket': data.get('ticket_hash', '')[:8]
            }
        elif response.status_code == 429:
            return {
                'seat': f"WLA-{seat_num}",
                'status': 'RATE_LIMITED',
                'time': elapsed,
                'error': data.get('error', 'Rate limited')
            }
        else:
            return {
                'seat': f"WLA-{seat_num}",
                'status': 'FAILED',
                'time': elapsed,
                'error': data.get('error', 'Unknown error')
            }
    except Exception as e:
        elapsed = time.time() - start
        return {
            'seat': f"WLA-{seat_num}",
            'status': 'ERROR',
            'time': elapsed,
            'error': str(e)
        }


def run_benchmark(base_url, num_workers=10, total_seats=80, bypass_ratelimit=False):
    """Run the benchmark with concurrent workers"""
    
    print(f"\n{'='*60}")
    print(f"SEAT BOOKING BENCHMARK")
    print(f"{'='*60}")
    print(f"Target: {base_url}")
    print(f"Workers: {num_workers}")
    print(f"Total seats to book: {total_seats} (WLA region)")
    print(f"Bypass rate limit: {bypass_ratelimit}")
    print(f"{'='*60}\n")
    
    # Prepare seat list
    seats_to_book = WLA_SEATS[:total_seats]
    
    results = {
        'success': 0,
        'failed': 0,
        'rate_limited': 0,
        'error': 0,
        'times': []
    }
    
    start_time = time.time()
    
    # Process in batches of num_workers
    for batch_start in range(0, len(seats_to_book), num_workers):
        batch = seats_to_book[batch_start:batch_start + num_workers]
        batch_num = (batch_start // num_workers) + 1
        
        print(f"Batch {batch_num}: Booking seats {batch[0]}-{batch[-1]}...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            args_list = [(seat, base_url, bypass_ratelimit) for seat in batch]
            futures = executor.map(book_seat, args_list)
            
            for result in futures:
                status = result['status']
                if status == 'SUCCESS':
                    results['success'] += 1
                    results['times'].append(result['time'])
                    print(f"  ✓ {result['seat']} - {result['time']:.3f}s - ticket: {result.get('ticket', '')}")
                elif status == 'RATE_LIMITED':
                    results['rate_limited'] += 1
                    print(f"  ⏳ {result['seat']} - RATE LIMITED")
                elif status == 'FAILED':
                    results['failed'] += 1
                    print(f"  ✗ {result['seat']} - {result.get('error', '')}")
                else:
                    results['error'] += 1
                    print(f"  ! {result['seat']} - ERROR: {result.get('error', '')}")
        
        # Small delay between batches to avoid overwhelming
        if batch_start + num_workers < len(seats_to_book):
            time.sleep(0.5)
    
    total_time = time.time() - start_time
    
    # Summary
    print(f"\n{'='*60}")
    print(f"RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"Total time: {total_time:.2f}s")
    print(f"Successful: {results['success']}")
    print(f"Failed: {results['failed']}")
    print(f"Rate limited: {results['rate_limited']}")
    print(f"Errors: {results['error']}")
    
    if results['times']:
        avg_time = sum(results['times']) / len(results['times'])
        min_time = min(results['times'])
        max_time = max(results['times'])
        print(f"\nResponse times:")
        print(f"  Avg: {avg_time:.3f}s")
        print(f"  Min: {min_time:.3f}s")
        print(f"  Max: {max_time:.3f}s")
    
    print(f"{'='*60}\n")
    
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Benchmark seat booking API')
    parser.add_argument('--url', default='http://localhost:8080', help='Base URL of the app')
    parser.add_argument('--workers', type=int, default=10, help='Number of concurrent workers')
    parser.add_argument('--seats', type=int, default=80, help='Number of seats to book (max 80 for WLA)')
    parser.add_argument('--no-ratelimit', action='store_true', help='Bypass rate limiting for benchmark')
    
    args = parser.parse_args()
    
    run_benchmark(args.url, args.workers, min(args.seats, 80), args.no_ratelimit)
