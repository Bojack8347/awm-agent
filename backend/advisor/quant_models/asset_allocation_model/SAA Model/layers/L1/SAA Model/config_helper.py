"""
Configuration Helper for SAA Model

Simple solution to ensure config parameter changes take effect.
This approach is clean, reliable, and avoids complex import manipulation.

Usage:
- Edit config.py parameters as needed
- Run your SAA script (it will pick up the changes)
- If using interactive Python/Jupyter, restart the kernel
"""

def print_current_config():
    """Print all current configuration parameters"""
    import config
    
    print("\n🔧 Current SAA Configuration Parameters:")
    print("=" * 50)
    print(f"📁 File Paths:")
    print(f"   DATA_FILE_PATH: {config.DATA_FILE_PATH}")
    print(f"   OUTPUT_FILE_PATH: {config.OUTPUT_FILE_PATH}")
    
    print(f"\n⚙️  Optimization Parameters:")
    print(f"   LIQUIDITY_TARGET: {config.LIQUIDITY_TARGET:.1%}")
    print(f"   LIQUIDITY_MODE: {getattr(config, 'LIQUIDITY_MODE', 'fixed_post')}")
    print(f"   ACTIVE_RISK_BUDGET: {config.ACTIVE_RISK_BUDGET:.1%}")
    print(f"   LAMBDA_ACTIVE: {config.LAMBDA_ACTIVE}")
    print(f"   LOOKBACK_YEARS: {config.LOOKBACK_YEARS}")
    print(f"   MAX_OPTIMIZATION_ITERATIONS: {config.MAX_OPTIMIZATION_ITERATIONS}")
    
    print(f"\n📊 Risk Management:")
    print(f"   RISK_TOLERANCE: {config.RISK_TOLERANCE}")
    print(f"   DYNAMIC_RISK_TOLERANCE: {config.DYNAMIC_RISK_TOLERANCE}")
    print(f"   OUTLIER_THRESHOLD: {config.OUTLIER_THRESHOLD}")
    
    print(f"\n🔢 Numerical Stability:")
    print(f"   MIN_EIGENVALUE_THRESHOLD: {config.MIN_EIGENVALUE_THRESHOLD}")
    print(f"   MATRIX_REGULARIZATION: {config.MATRIX_REGULARIZATION}")
    print(f"   CONVERGENCE_TOLERANCE: {config.CONVERGENCE_TOLERANCE}")
    print(f"   TIGHT_CONVERGENCE_TOLERANCE: {config.TIGHT_CONVERGENCE_TOLERANCE}")
    
    print(f"\n🎯 Optimization Strategy:")
    print(f"   NUM_OPTIMIZATION_ATTEMPTS: {config.NUM_OPTIMIZATION_ATTEMPTS}")
    print(f"   CLUSTER_BUDGET_FACTOR_MIN: {config.CLUSTER_BUDGET_FACTOR_MIN}")
    print(f"   CLUSTER_BUDGET_FACTOR_MAX: {config.CLUSTER_BUDGET_FACTOR_MAX}")
    print("=" * 50)

def validate_config():
    """Validate that configuration parameters make sense"""
    import config
    
    print("\n✅ Validating Configuration Parameters...")
    
    # Basic validations
    issues = []
    
    if config.LOOKBACK_YEARS <= 0:
        issues.append(f"LOOKBACK_YEARS must be positive (current: {config.LOOKBACK_YEARS})")
    
    if not (0 < config.LIQUIDITY_TARGET < 1):
        issues.append(f"LIQUIDITY_TARGET must be between 0 and 1 (current: {config.LIQUIDITY_TARGET})")
    
    if not (0 < config.ACTIVE_RISK_BUDGET < 1):
        issues.append(f"ACTIVE_RISK_BUDGET must be between 0 and 1 (current: {config.ACTIVE_RISK_BUDGET})")
    
    if config.LAMBDA_ACTIVE <= 0:
        issues.append(f"LAMBDA_ACTIVE must be positive (current: {config.LAMBDA_ACTIVE})")
    
    if config.MAX_OPTIMIZATION_ITERATIONS <= 0:
        issues.append(f"MAX_OPTIMIZATION_ITERATIONS must be positive (current: {config.MAX_OPTIMIZATION_ITERATIONS})")
    
    if issues:
        print("❌ Configuration Issues Found:")
        for issue in issues:
            print(f"   - {issue}")
        return False
    else:
        print("✅ All configuration parameters are valid")
        return True

if __name__ == "__main__":
    print("🚀 SAA Configuration Helper")
    print_current_config()
    validate_config()
    
    print(f"\n📝 To change parameters:")
    print(f"1. Edit config.py")
    print(f"2. Run your SAA script")
    print(f"3. Changes will be applied!")
