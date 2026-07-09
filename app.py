# app.py

import streamlit as st
import pandas as pd
import numpy as np
import os
import io
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Supabase
url: str = os.environ.get("SUPABASE_URL", os.environ.get("VITE_SUPABASE_URL", ""))
key: str = os.environ.get("SUPABASE_ANON_KEY", os.environ.get("VITE_SUPABASE_ANON_KEY", ""))
supabase: Client = create_client(url, key) if url and key else None

from datetime import datetime

# =========================================================
# DATABASE HELPERS
# =========================================================

def prepare_db_records(m_i, m_c, m_s, u_l_i, u_l_c, u_l_s, u_b_i, u_b_c, u_b_s):
    def format_unmatched_ledger(df, tax_label):
        if df.empty: return pd.DataFrame()
        res = pd.DataFrame()
        res['GSTIN'] = df['GSTIN'].astype(str).str.strip()
        res['Bill/Inv No'] = df['Bill No'].astype(str).str.strip()
        res['Ledger Amt'] = pd.to_numeric(df['Debit Amount-INR'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
        res['B2B Amt'] = 0.0
        res['Diff'] = res['Ledger Amt']
        res['Type'] = tax_label
        res['Status'] = '❌ Mismatch'
        res['Invoice Date'] = ""
        res['Filing Date'] = ""
        res['Account'] = df['Account'].astype(str)
        res['Bill Date'] = df['Bill Date'].astype(str)
        res['Trade Name'] = ""
        res['Invoice Value'] = 0.0
        res['VC Date'] = df['VC Date'].astype(str) if 'VC Date' in df.columns else ""
        res['VC No'] = df['VC No'].astype(str) if 'VC No' in df.columns else ""
        return res

    def format_unmatched_b2b(df, tax_label, amt_col):
        if df.empty: return pd.DataFrame()
        res = pd.DataFrame()
        res['GSTIN'] = df['GSTIN of supplier'].astype(str).str.strip()
        res['Bill/Inv No'] = df['Invoice number'].astype(str).str.strip()
        res['Ledger Amt'] = 0.0
        res['B2B Amt'] = pd.to_numeric(df[amt_col].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
        res['Diff'] = -res['B2B Amt']
        res['Type'] = tax_label
        res['Status'] = '❌ Mismatch'
        res['Invoice Date'] = df.get('Invoice Date', "").astype(str)
        
        filing_date_col = next((c for c in df.columns if 'Filing Date' in str(c)), None)
        res['Filing Date'] = df[filing_date_col].astype(str) if filing_date_col else ""
        
        res['Account'] = ""
        res['Bill Date'] = ""
        res['Trade Name'] = df.get('Trade/Legal name', "").astype(str)
        res['Invoice Value'] = pd.to_numeric(df.get('Invoice Value(₹)', 0), errors='coerce').fillna(0)
        res['VC Date'] = ""
        res['VC No'] = ""
        return res

    all_dfs = [m_i, m_c, m_s, 
               format_unmatched_ledger(u_l_i, 'IGST'), 
               format_unmatched_ledger(u_l_c, 'CGST'), 
               format_unmatched_ledger(u_l_s, 'SGST'),
               format_unmatched_b2b(u_b_i, 'IGST', 'Integrated Tax(₹)'),
               format_unmatched_b2b(u_b_c, 'CGST', 'Central Tax(₹)'),
               format_unmatched_b2b(u_b_s, 'SGST', 'State/UT Tax(₹)')]
               
    non_empty_dfs = [d for d in all_dfs if not d.empty]
    if not non_empty_dfs: return pd.DataFrame()
    combined = pd.concat(non_empty_dfs, ignore_index=True)
    if combined.empty: return pd.DataFrame()
    
    # Fill NaN values to prevent pivot_table from silently dropping rows with missing metadata
    combined.fillna({
        'GSTIN': '', 'Bill/Inv No': '', 'Account': '', 'Bill Date': '', 
        'Invoice Date': '', 'Filing Date': '', 'Trade Name': '', 
        'Invoice Value': 0.0, 'VC Date': '', 'VC No': ''
    }, inplace=True)
    
    pivot_df = combined.pivot_table(
        index=[
            'GSTIN', 'Bill/Inv No', 'Account', 'Bill Date', 
            'Invoice Date', 'Filing Date', 'Trade Name', 'Invoice Value', 
            'VC Date', 'VC No'
        ],
        columns='Type',
        values=['Ledger Amt', 'B2B Amt', 'Diff'],
        aggfunc='sum'
    ).reset_index()
    
    pivot_df.columns = [f"{col[0]} {col[1]}".strip() if col[1] else col[0] for col in pivot_df.columns.values]
    pivot_df.fillna(0, inplace=True)
    
    diff_cols = [c for c in pivot_df.columns if 'Diff' in c]
    ledger_cols = [c for c in pivot_df.columns if 'Ledger Amt' in c]
    b2b_cols = [c for c in pivot_df.columns if 'B2B Amt' in c]
    
    def get_status(row):
        tot_l = sum(abs(float(row[c])) for c in ledger_cols if pd.notna(row[c]))
        tot_b = sum(abs(float(row[c])) for c in b2b_cols if pd.notna(row[c]))
        
        has_diff = False
        for c in diff_cols:
            if pd.notna(row[c]) and abs(row[c]) >= 1:
                has_diff = True
                break
                
        if has_diff:
            if tot_b == 0 and tot_l > 0:
                return '❌ Missing (in B2B)'
            elif tot_l == 0 and tot_b > 0:
                return '❌ Missing (in Ledger)'
            else:
                if tot_l >= tot_b:
                    return '❌ Missing (in B2B)'
                else:
                    return '❌ Missing (in Ledger)'
        return '✅ Match'
    pivot_df['Status'] = pivot_df.apply(get_status, axis=1)
    
    db_rows = []
    for _, row in pivot_df.iterrows():
        status = row['Status']
        db_rows.append({
            "GSTIN": str(row['GSTIN']),
            "Bill/Inv No": str(row['Bill/Inv No']),
            "Bill Date": str(row['Bill Date']),
            "Account": str(row['Account']),
            "Trade Name": str(row.get('Trade Name', '')),
            "Invoice Date": str(row.get('Invoice Date', '')),
            "Filing Date": str(row.get('Filing Date', '')),
            "Invoice Value": float(row.get('Invoice Value', 0)),
            "VC Date": str(row.get('VC Date', '')),
            "VC No": str(row.get('VC No', '')),
            "Ledger Amt IGST": float(row.get('Ledger Amt IGST', 0)),
            "B2B Amt IGST": float(row.get('B2B Amt IGST', 0)),
            "Diff IGST": float(row.get('Diff IGST', 0)),
            "Ledger Amt CGST": float(row.get('Ledger Amt CGST', 0)),
            "B2B Amt CGST": float(row.get('B2B Amt CGST', 0)),
            "Diff CGST": float(row.get('Diff CGST', 0)),
            "Ledger Amt SGST": float(row.get('Ledger Amt SGST', 0)),
            "B2B Amt SGST": float(row.get('B2B Amt SGST', 0)),
            "Diff SGST": float(row.get('Diff SGST', 0)),
            "Status": status
        })
    return pd.DataFrame(db_rows)

def save_all_to_supabase(m_i, m_c, m_s, u_l_i, u_l_c, u_l_s, u_b_i, u_b_c, u_b_s):
    if not supabase: return
    db_df = prepare_db_records(m_i, m_c, m_s, u_l_i, u_l_c, u_l_s, u_b_i, u_b_c, u_b_s)
    if db_df.empty: return
    
    now_str = datetime.now().isoformat()
    rows = []
    for _, row in db_df.iterrows():
        status = row['Status']
        rows.append({
            "gstin": str(row['GSTIN']),
            "bill_no": str(row['Bill/Inv No']),
            "bill_date": str(row['Bill Date']),
            "account": str(row['Account']),
            "trade_name": str(row['Trade Name']),
            "invoice_date": str(row['Invoice Date']),
            "filing_date": str(row['Filing Date']),
            "invoice_value": float(row['Invoice Value']),
            "vc_date": str(row['VC Date']),
            "vc_no": str(row['VC No']),
            "igst_ledger_amt": float(row['Ledger Amt IGST']),
            "cgst_ledger_amt": float(row['Ledger Amt CGST']),
            "sgst_ledger_amt": float(row['Ledger Amt SGST']),
            "igst_b2b_amt": float(row['B2B Amt IGST']),
            "cgst_b2b_amt": float(row['B2B Amt CGST']),
            "sgst_b2b_amt": float(row['B2B Amt SGST']),
            "status": status,
            "reconciled_date": now_str if status == '✅ Match' else None
        })
        
    try:
        supabase.table("gst_pending_ledger").upsert(rows).execute()
        st.success(f"☁️ Successfully consolidated and saved {len(rows)} invoices to Supabase.")
    except Exception as e:
        st.error(f"Save Error: {e}")

def fetch_pending_leftovers():
    if not supabase: return pd.DataFrame()
    try:
        res = supabase.table("gst_pending_ledger").select("*").neq("status", "✅ Match").execute()
        return pd.DataFrame(res.data) if res.data else pd.DataFrame()
    except Exception as e:
        st.error(f"Fetch Error: {e}")
        return pd.DataFrame()

def mark_reconciled_in_supabase(gstin, bill_no):
    if not supabase: return
    try:
        now_str = datetime.now().isoformat()
        supabase.table("gst_pending_ledger").update({"status": "✅ Match", "reconciled_date": now_str}).match({"gstin": gstin, "bill_no": bill_no}).execute()
    except Exception as e:
        st.error(f"Update Error: {e}")

def fetch_all_records_from_supabase():
    if not supabase: return pd.DataFrame()
    try:
        res = supabase.table("gst_pending_ledger").select("*").execute()
        return pd.DataFrame(res.data) if res.data else pd.DataFrame()
    except Exception as e:
        st.error(f"Fetch All Error: {e}")
        return pd.DataFrame()

def parse_to_month_year(date_str):
    if not date_str or pd.isna(date_str) or str(date_str).strip() == "":
        return "Unknown Month"
    # Try common formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(str(date_str).strip().split(" ")[0], fmt)
            return dt.strftime("%B %Y")
        except ValueError:
            continue
    try:
        dt = pd.to_datetime(date_str, errors='coerce')
        if pd.notna(dt):
            return dt.strftime("%B %Y")
    except:
        pass
    return "Unknown Month"

def group_by_month(df, date_col):
    if df.empty:
        return {}
    df = df.copy()
    df['Month_Year'] = df[date_col].apply(parse_to_month_year)
    groups = {}
    for month, group in df.groupby('Month_Year'):
        groups[month] = group.drop(columns=['Month_Year'])
    return groups

def sort_month_keys(keys):
    def get_sort_key(k):
        if k == "Unknown Month":
            return datetime.min
        try:
            return datetime.strptime(k, "%B %Y")
        except:
            return datetime.min
    return sorted(list(keys), key=get_sort_key, reverse=True)

def extract_dataframes_from_leftovers(prev_leftovers, filing_date_col_name="GSTR-1/1A/IFF/GSTR-5 Filing Date"):
    if prev_leftovers.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    
    # 1. IGST Ledger
    db_l_i = prev_leftovers[pd.to_numeric(prev_leftovers['igst_ledger_amt'], errors='coerce').fillna(0) > 0].copy()
    if not db_l_i.empty:
        db_ledger_i = pd.DataFrame({
            'GSTIN': db_l_i['gstin'].astype(str).str.strip(),
            'Bill Date': db_l_i['bill_date'].astype(str).str.strip(),
            'Bill No': db_l_i['bill_no'].astype(str).str.strip(),
            'Account': db_l_i['account'].astype(str).str.strip(),
            'Debit Amount-INR': pd.to_numeric(db_l_i['igst_ledger_amt'], errors='coerce').fillna(0),
            'VC Date': db_l_i['vc_date'].astype(str).str.strip(),
            'VC No': db_l_i['vc_no'].astype(str).str.strip()
        })
    else:
        db_ledger_i = pd.DataFrame(columns=['GSTIN', 'Bill Date', 'Bill No', 'Account', 'Debit Amount-INR', 'VC Date', 'VC No'])

    # 2. CGST Ledger
    db_l_c = prev_leftovers[pd.to_numeric(prev_leftovers['cgst_ledger_amt'], errors='coerce').fillna(0) > 0].copy()
    if not db_l_c.empty:
        db_ledger_c = pd.DataFrame({
            'GSTIN': db_l_c['gstin'].astype(str).str.strip(),
            'Bill Date': db_l_c['bill_date'].astype(str).str.strip(),
            'Bill No': db_l_c['bill_no'].astype(str).str.strip(),
            'Account': db_l_c['account'].astype(str).str.strip(),
            'Debit Amount-INR': pd.to_numeric(db_l_c['cgst_ledger_amt'], errors='coerce').fillna(0),
            'VC Date': db_l_c['vc_date'].astype(str).str.strip(),
            'VC No': db_l_c['vc_no'].astype(str).str.strip()
        })
    else:
        db_ledger_c = pd.DataFrame(columns=['GSTIN', 'Bill Date', 'Bill No', 'Account', 'Debit Amount-INR', 'VC Date', 'VC No'])

    # 3. SGST Ledger
    db_l_s = prev_leftovers[pd.to_numeric(prev_leftovers['sgst_ledger_amt'], errors='coerce').fillna(0) > 0].copy()
    if not db_l_s.empty:
        db_ledger_s = pd.DataFrame({
            'GSTIN': db_l_s['gstin'].astype(str).str.strip(),
            'Bill Date': db_l_s['bill_date'].astype(str).str.strip(),
            'Bill No': db_l_s['bill_no'].astype(str).str.strip(),
            'Account': db_l_s['account'].astype(str).str.strip(),
            'Debit Amount-INR': pd.to_numeric(db_l_s['sgst_ledger_amt'], errors='coerce').fillna(0),
            'VC Date': db_l_s['vc_date'].astype(str).str.strip(),
            'VC No': db_l_s['vc_no'].astype(str).str.strip()
        })
    else:
        db_ledger_s = pd.DataFrame(columns=['GSTIN', 'Bill Date', 'Bill No', 'Account', 'Debit Amount-INR', 'VC Date', 'VC No'])

    # 4. B2B
    db_b = prev_leftovers[
        (pd.to_numeric(prev_leftovers['igst_b2b_amt'], errors='coerce').fillna(0) > 0) |
        (pd.to_numeric(prev_leftovers['cgst_b2b_amt'], errors='coerce').fillna(0) > 0) |
        (pd.to_numeric(prev_leftovers['sgst_b2b_amt'], errors='coerce').fillna(0) > 0)
    ].copy()
    if not db_b.empty:
        db_b2b = pd.DataFrame({
            'GSTIN of supplier': db_b['gstin'].astype(str).str.strip(),
            'Invoice number': db_b['bill_no'].astype(str).str.strip(),
            'Trade/Legal name': db_b['trade_name'].astype(str).str.strip(),
            'Invoice Date': db_b['invoice_date'].astype(str).str.strip(),
            'Invoice Value(₹)': pd.to_numeric(db_b['invoice_value'], errors='coerce').fillna(0),
            'Integrated Tax(₹)': pd.to_numeric(db_b['igst_b2b_amt'], errors='coerce').fillna(0),
            'Central Tax(₹)': pd.to_numeric(db_b['cgst_b2b_amt'], errors='coerce').fillna(0),
            'State/UT Tax(₹)': pd.to_numeric(db_b['sgst_b2b_amt'], errors='coerce').fillna(0),
            filing_date_col_name: db_b['filing_date'].astype(str).str.strip()
        })
    else:
        db_b2b = pd.DataFrame(columns=[
            'GSTIN of supplier', 'Invoice number', 'Trade/Legal name', 'Invoice Date',
            'Invoice Value(₹)', 'Integrated Tax(₹)', 'Central Tax(₹)', 'State/UT Tax(₹)',
            filing_date_col_name
        ])

    return db_ledger_i, db_ledger_c, db_ledger_s, db_b2b

def merge_and_deduplicate(uploaded_df, db_df, keys):
    if uploaded_df is None or uploaded_df.empty:
        return db_df
    if db_df is None or db_df.empty:
        return uploaded_df
    uploaded_df = uploaded_df.copy()
    db_df = db_df.copy()
    for k in keys:
        if k in uploaded_df.columns:
            uploaded_df[k] = uploaded_df[k].astype(str).str.strip()
        if k in db_df.columns:
            db_df[k] = db_df[k].astype(str).str.strip()
    combined = pd.concat([uploaded_df, db_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=keys, keep='first')
    return combined


# =========================================================
# PAGE CONFIG
# =========================================================

st.set_page_config(
    page_title="GST - Ledger Reconciliation Portal",
    page_icon="📊",
    layout="wide"
)

# =========================================================
# CUSTOM CSS
# =========================================================

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

*, *::before, *::after { box-sizing: border-box; }

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}

[data-testid="stAppViewContainer"] {
    background-color: #f4f7fa;
}

.block-container {
    padding-top: 2rem;
    padding-bottom: 3rem;
    max-width: 97%;
}

/* ---- HEADER ---- */
.header-box {
    background: linear-gradient(135deg, #0a2540 0%, #113052 100%);
    padding: 36px 40px;
    border-radius: 16px;
    margin-bottom: 32px;
    box-shadow: 0 10px 25px -5px rgba(10, 37, 64, 0.2);
    position: relative;
    overflow: hidden;
    text-align: center;
}
.header-box::before {
    content: '';
    position: absolute;
    top: -60px; right: -40px;
    width: 250px; height: 250px;
    background: radial-gradient(circle, rgba(99,179,237,0.15) 0%, transparent 70%);
    border-radius: 50%;
}
.header-logo {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 2px;
    color: #63b3ed;
    text-transform: uppercase;
    margin-bottom: 12px;
}
.header-title {
    font-size: 34px;
    font-weight: 800;
    color: #ffffff;
    margin-bottom: 8px;
    letter-spacing: -0.5px;
    line-height: 1.2;
}
.header-sub {
    color: #a0aec0;
    font-size: 15px;
    font-weight: 400;
    margin-top: 4px;
}
.header-badges {
    display: flex;
    gap: 10px;
    margin-top: 20px;
    flex-wrap: wrap;
}
.badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: rgba(255,255,255,0.1);
    color: #e2e8f0;
    border: 1px solid rgba(255,255,255,0.15);
    font-size: 12px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 6px;
}

/* ---- SECTION HEADINGS ---- */
.section-header {
    display: flex;
    align-items: center;
    gap: 14px;
    margin: 40px 0 20px;
    padding-bottom: 16px;
    border-bottom: 2px solid #e2e8f0;
}
.section-icon {
    width: 44px; height: 44px;
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px;
    flex-shrink: 0;
    color: white;
}
.section-icon.blue  { background: linear-gradient(135deg,#0052cc,#2684ff); box-shadow: 0 4px 10px rgba(0,82,204,0.2); }
.section-icon.teal  { background: linear-gradient(135deg,#00875a,#36b37e); box-shadow: 0 4px 10px rgba(0,135,90,0.2); }
.section-icon.green { background: linear-gradient(135deg,#00875a,#36b37e); box-shadow: 0 4px 10px rgba(0,135,90,0.2); }
.section-title { font-size: 24px; font-weight: 700; color: #0a2540; }
.section-sub   { font-size: 14px; color: #475569; margin-top: 2px; }

/* ---- TAX TYPE PILL ---- */
.tax-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 700;
    margin-bottom: 16px;
}
.tax-pill.igst { background: #ebf8ff; color: #2b6cb0; border: 1px solid #bee3f8; }
.tax-pill.cgst { background: #faf5ff; color: #6b46c1; border: 1px solid #e9d8fd; }
.tax-pill.sgst { background: #e6fffa; color: #285e61; border: 1px solid #b2f5ea; }

/* ---- METRIC CARDS ---- */
.metric-row { display: flex; gap: 16px; margin: 20px 0 28px; flex-wrap: wrap; }
.metric-card {
    flex: 1; min-width: 130px;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.02);
}
.metric-value { font-size: 32px; font-weight: 800; color: #0a2540; line-height: 1.1; }
.metric-label { font-size: 12px; font-weight: 600; color: #64748b; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
.metric-card.green .metric-value { color: #059669; }
.metric-card.red   .metric-value { color: #dc2626; }
.metric-card.blue  .metric-value { color: #2563eb; }
.metric-card.amber .metric-value { color: #d97706; }

/* ---- DIVIDER ---- */
.styled-divider {
    height: 2px;
    background: #e2e8f0;
    margin: 40px 0;
    border: none;
}

/* ---- DOWNLOAD BANNER ---- */
.download-banner {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-left: 4px solid #2563eb;
    border-radius: 12px;
    padding: 24px 30px;
    margin-top: 24px;
    display: flex;
    align-items: center;
    gap: 24px;
    box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);
}
.download-icon {
    font-size: 32px;
    width: 60px; height: 60px;
    background: #eff6ff;
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    color: #2563eb;
}
.download-text-title { font-size: 18px; font-weight: 700; color: #0f172a; }
.download-text-sub   { font-size: 14px; color: #64748b; margin-top: 4px; }

/* ---- STREAMLIT OVERRIDES ---- */
[data-testid="stFileUploader"] {
    background: #ffffff !important;
    border-radius: 12px !important;
    padding: 1rem !important;
    border: 1px solid #e2e8f0 !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
}
[data-testid="stFileUploader"] > div {
    background: #f8fafc !important;
    border: 2px dashed #cbd5e1 !important;
    border-radius: 8px !important;
    transition: all 0.2s !important;
}
[data-testid="stFileUploader"] > div:hover {
    border-color: #3b82f6 !important;
    background: #eff6ff !important;
}
[data-testid="stTabs"] [role="tab"] {
    font-weight: 600 !important;
    font-size: 14px !important;
    color: #64748b !important;
    padding: 10px 20px !important;
    border-radius: 8px 8px 0 0 !important;
    transition: color 0.2s !important;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    color: #2563eb !important;
    border-bottom: 3px solid #2563eb !important;
}
[data-testid="stDataFrame"] {
    border: 1px solid #e2e8f0 !important;
    border-radius: 8px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
}
button[kind="secondary"] {
    background: #ffffff !important;
    border: 1px solid #cbd5e1 !important;
    color: #0f172a !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    padding: 0.5rem 1rem !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.05) !important;
}
button[kind="secondary"]:hover {
    background: #f8fafc !important;
    border-color: #94a3b8 !important;
}
[data-testid="stExpander"] {
    background: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 12px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
}
h1, h2, h3, h4, h5, h6 { color: #0a2540 !important; }

</style>
""", unsafe_allow_html=True)

# =========================================================
# HEADER
# =========================================================

st.markdown("""
<div class="header-box">
    <div class="header-title">GST - Ledger Reconciliation Portal</div>
</div>
""", unsafe_allow_html=True)

# =========================================================
# FILE UPLOADS
# =========================================================

main_tab1, main_tab2 = st.tabs(["📤 Upload & Reconcile", "🗄️ Central Database View"])

with main_tab1:
    col1, col2 = st.columns(2)

    with col1:
        company_ledger_file = st.file_uploader(
            "Upload Company Ledger",
            type=["xlsx", "xls"]
        )

    with col2:
        gst_records_file = st.file_uploader(
            "Upload GST Records",
            type=["xlsx", "xls"]
        )

    # =========================================================
    # HELPERS
    # =========================================================

    def detect_company_ledger_header(df):
        required_headers = [
            "GSTIN", "VC Date", "VC No", "Account", 
            "Bill Date", "Bill No", "Debit Amount-INR", "Narration"
        ]
        for index, row in df.iterrows():
            row_values = [str(value).strip() for value in row.values]
            matched_count = 0
            for header in required_headers:
                for cell in row_values:
                    if header.lower() == cell.lower():
                        matched_count += 1
                        break
            if matched_count >= 6:
                return index
        return None

    def split_gst_sections(df):
        igst_start, sgst_start, cgst_start = None, None, None
        for idx, row in df.iterrows():
            row_text = " ".join([str(value).strip() for value in row.values if pd.notna(value)])
            if "IGST Account" in row_text: igst_start = idx
            elif "SGST Account" in row_text: sgst_start = idx
            elif "CGST Account" in row_text: cgst_start = idx

        def clean_section(section_df):
            opening_balance, total_value = None, None
            cleaned_rows = []
            debit_col = None
            for col in section_df.columns:
                if "Debit Amount" in str(col):
                    debit_col = col
                    break
            for _, row in section_df.iterrows():
                row_text = " ".join([str(value).strip() for value in row.values if pd.notna(value)])
                if "Opening Balance" in row_text:
                    if debit_col: opening_balance = row[debit_col]
                    continue
                elif "Total" in row_text:
                    if debit_col: total_value = row[debit_col]
                    continue
                cleaned_rows.append(row)
            cleaned_df = pd.DataFrame(cleaned_rows).dropna(how="all")
            return cleaned_df, opening_balance, total_value

        igst_df = df.iloc[igst_start + 1 : sgst_start].copy() if igst_start is not None and sgst_start is not None else pd.DataFrame()
        sgst_df = df.iloc[sgst_start + 1 : cgst_start].copy() if sgst_start is not None and cgst_start is not None else pd.DataFrame()
        cgst_df = df.iloc[cgst_start + 1 :].copy() if cgst_start is not None else pd.DataFrame()

        i_df, i_op, i_tot = clean_section(igst_df)
        s_df, s_op, s_tot = clean_section(sgst_df)
        c_df, c_op, c_tot = clean_section(cgst_df)

        return i_df, s_df, c_df, i_op, i_tot, s_op, s_tot, c_op, c_tot

    # =========================================================
    # PROCESS FILES
    # =========================================================

    if company_ledger_file:
        try:
            raw_ledger_df = pd.read_excel(company_ledger_file, header=None)
            h_row = detect_company_ledger_header(raw_ledger_df)
            if h_row is not None:
                clean_ledger_df = pd.read_excel(company_ledger_file, header=h_row)
                clean_ledger_df.columns = [str(col).strip() for col in clean_ledger_df.columns]
                req_cols = ["GSTIN", "VC Date", "VC No", "Account", "Bill Date", "Bill No", "Debit Amount-INR"]
                filtered_cols = [c for r in req_cols for c in clean_ledger_df.columns if r.lower() == str(c).strip().lower()]
                clean_ledger_df = clean_ledger_df[filtered_cols]
                (igst_l, sgst_l, cgst_l, i_op, i_tot, s_op, s_tot, c_op, c_tot) = split_gst_sections(clean_ledger_df)
                st.session_state['igst_ledger'] = igst_l
                st.session_state['sgst_ledger'] = sgst_l
                st.session_state['cgst_ledger'] = cgst_l
                with st.expander("🗂️  View Parsed Ledger (IGST / SGST / CGST)", expanded=False):
                    t_a, t_b, t_c = st.tabs(["IGST Ledger", "SGST Ledger", "CGST Ledger"])
                    with t_a: st.dataframe(igst_l, use_container_width=True, hide_index=True)
                    with t_b: st.dataframe(sgst_l, use_container_width=True, hide_index=True)
                    with t_c: st.dataframe(cgst_l, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Ledger Error: {e}")

    if gst_records_file:
        try:
            xl = pd.ExcelFile(gst_records_file)
            b2b_s = next((s for s in xl.sheet_names if s.strip().lower() == "b2b"), None)
            if b2b_s:
                raw_b2b_df = pd.read_excel(gst_records_file, sheet_name=b2b_s, header=None)
                req_h = [
                    "GSTIN of supplier", "Invoice number", "Integrated Tax(₹)", "Central Tax(₹)", 
                    "State/UT Tax(₹)", "Trade/Legal name", "Invoice Date", 
                    "Invoice Value(₹)", "GSTR-1/1A/IFF/GSTR-5 Filing Date"
                ]
                h_map, h_rows = {}, []
                sorted_h = sorted(req_h, key=len, reverse=True)
                for c_idx in range(raw_b2b_df.shape[1]):
                    for r_idx in range(min(15, len(raw_b2b_df))):
                        val = str(raw_b2b_df.iloc[r_idx, c_idx]).strip().lower()
                        for k in sorted_h:
                            if k.lower() in val or val in k.lower():
                                h_map[c_idx], h_rows = k, h_rows + [r_idx]
                                break
                        if c_idx in h_map: break
                if h_rows:
                    f_h = [h_map.get(i, f"Col_{i}") for i in range(raw_b2b_df.shape[1])]
                    c_h, h_cnt = [], {}
                    for h in f_h:
                        if h in h_cnt: h_cnt[h] += 1; h = f"{h}_{h_cnt[h]}"
                        else: h_cnt[h] = 1
                        c_h.append(h)
                    b2b_df = raw_b2b_df.iloc[max(h_rows)+1:].copy()
                    b2b_df.columns = c_h
                    b2b_df = b2b_df.dropna(how="all").astype(str).replace("nan", "")
                    st.session_state['b2b_data'] = b2b_df
                    with st.expander("🗂️  View Parsed B2B Records", expanded=False):
                        st.dataframe(b2b_df, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"GST Error: {e}")

    # =========================================================
    # RECONCILIATION
    # =========================================================

    if 'igst_ledger' in st.session_state and 'b2b_data' in st.session_state:

        b2b = st.session_state['b2b_data'].copy()

        # Ensure numeric columns
        for col in ['Integrated Tax(₹)', 'Central Tax(₹)', 'State/UT Tax(₹)']:
            if col in b2b.columns:
                b2b[col] = pd.to_numeric(b2b[col].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

        # Find Filing Date column
        filing_date_col = next((c for c in b2b.columns if 'Filing Date' in str(c)), None)

        def run_recon(ledger_df, b2b_df, b_amt_col, label):
            if ledger_df.empty: return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

            l_df = ledger_df.copy()
            l_df['Debit Amount-INR'] = pd.to_numeric(l_df['Debit Amount-INR'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

            # 1. Filter B2B where tax amount > 0
            if b_amt_col in b2b_df.columns:
                b_filtered = b2b_df[b2b_df[b_amt_col] > 0].copy()
            else:
                b_filtered = pd.DataFrame(columns=b2b_df.columns)

            # 2. Sort both by GSTIN and Invoice Number for predictable "Sort & Match"
            l_df = l_df.sort_values(by=['GSTIN', 'Bill No'], na_position='last')
            if 'Invoice number' in b_filtered.columns:
                b_filtered = b_filtered.sort_values(by=['GSTIN of supplier', 'Invoice number'], na_position='last')

            matches = []
            u_ledger = []
            matched_b_indices = set()

            for idx_l, row_l in l_df.iterrows():
                gstin_l = str(row_l['GSTIN']).strip()
                # Treat ledger amount as absolute
                amt_l = abs(float(row_l['Debit Amount-INR']))
                found = False

                # Try to match by both GSTIN and Invoice Number
                bill_no_l = str(row_l.get('Bill No', '')).strip().lower()

                for idx_b, row_b in b_filtered.iterrows():
                    if idx_b in matched_b_indices: continue

                    gstin_b = str(row_b['GSTIN of supplier']).strip()
                    inv_no_b = str(row_b.get('Invoice number', '')).strip().lower()

                    if gstin_b == gstin_l and (inv_no_b == bill_no_l or bill_no_l == ""):
                        amt_b = float(row_b[b_amt_col])
                        inv_date = row_b.get('Invoice Date', "")
                        date_val = row_b[filing_date_col] if filing_date_col else ""
                        inv_no_actual = row_b.get('Invoice number', "")

                        diff = amt_l - amt_b
                        matches.append({
                            'GSTIN': gstin_l, 
                            'Bill/Inv No': inv_no_actual if inv_no_actual else row_l.get('Bill No', ''),
                            'Ledger Amt': amt_l, 
                            'B2B Amt': amt_b,
                            'Diff': diff, 
                            'Type': label, 
                            'Status': '✅ Match' if abs(diff) < 1 else '❌ Mismatch',
                            'Invoice Date': inv_date,
                            'Filing Date': date_val,
                            'Account': row_l.get('Account', ''),
                            'Bill Date': row_l.get('Bill Date', ''),
                            'Trade Name': row_b.get('Trade/Legal name', ''),
                            'Invoice Value': float(row_b.get('Invoice Value(₹)', 0)) if row_b.get('Invoice Value(₹)', '') else 0.0,
                            'VC Date': row_l.get('VC Date', ''),
                            'VC No': row_l.get('VC No', '')
                        })
                        matched_b_indices.add(idx_b)
                        found = True
                        break 

                if not found: u_ledger.append(row_l)

            matched_df = pd.DataFrame(matches)

            # 2. Reorder columns to place Invoice Date before Filing Date
            if not matched_df.empty:
                cols = [
                    'GSTIN', 'Bill/Inv No', 'Ledger Amt', 'B2B Amt', 'Diff', 
                    'Type', 'Status', 'Invoice Date', 'Filing Date',
                    'Account', 'Bill Date', 'Trade Name', 'Invoice Value',
                    'VC Date', 'VC No'
                ]
                # Only keep columns that actually exist
                matched_df = matched_df[[c for c in cols if c in matched_df.columns]]

                # 3. Sort Matches by Filing Date
                if 'Filing Date' in matched_df.columns:
                    matched_df = matched_df.sort_values(by='Filing Date', na_position='last')

            unmatched_ledger_df = pd.DataFrame(u_ledger)
            unmatched_b2b_df = b_filtered.drop(index=list(matched_b_indices))

            return matched_df, unmatched_ledger_df, unmatched_b2b_df

        def style_status(df):
            if df.empty: return df
            return df.style.map(
                lambda v: 'color: #166534; font-weight: bold;' if '✅' in str(v) 
                else ('color: #991b1b; font-weight: bold;' if '❌' in str(v) else ''),
                subset=['Status']
            )

        # Fetch Pending Leftovers from Previous Months
        prev_leftovers = fetch_pending_leftovers()
        db_ledger_i, db_ledger_c, db_ledger_s, db_b2b = extract_dataframes_from_leftovers(prev_leftovers)

        # Merge database leftovers with uploaded data
        igst_ledger_combined = merge_and_deduplicate(st.session_state['igst_ledger'], db_ledger_i, keys=['GSTIN', 'Bill No'])
        cgst_ledger_combined = merge_and_deduplicate(st.session_state['cgst_ledger'], db_ledger_c, keys=['GSTIN', 'Bill No'])
        sgst_ledger_combined = merge_and_deduplicate(st.session_state['sgst_ledger'], db_ledger_s, keys=['GSTIN', 'Bill No'])

        b2b_combined = merge_and_deduplicate(b2b, db_b2b, keys=['GSTIN of supplier', 'Invoice number'])

        # Run for IGST, CGST, SGST on merged data
        m_i, u_l_i, u_b_i = run_recon(igst_ledger_combined, b2b_combined, 'Integrated Tax(₹)', 'IGST')
        m_c, u_l_c, u_b_c = run_recon(cgst_ledger_combined, b2b_combined, 'Central Tax(₹)', 'CGST')
        m_s, u_l_s, u_b_s = run_recon(sgst_ledger_combined, b2b_combined, 'State/UT Tax(₹)', 'SGST')

        st.markdown('<hr class="styled-divider">', unsafe_allow_html=True)
        st.markdown("""
        <div class="section-header">
            <div class="section-icon teal">📊</div>
            <div>
                <div class="section-title">Current Month Reconciliation</div>
                <div class="section-sub">Side-by-side comparison of Ledger vs. GSTR-2B B2B entries for IGST, CGST &amp; SGST</div>
            </div>
        </div>""", unsafe_allow_html=True)

        # Display Display Results Independently

        # --- IGST ---
        st.markdown('<div class="tax-pill igst">⚡ IGST &nbsp;—&nbsp; Integrated Tax</div>', unsafe_allow_html=True)
        # Metrics
        total_i = len(m_i); match_i = len(m_i[m_i['Status']=='✅ Match']) if not m_i.empty and 'Status' in m_i.columns else 0
        mm_i = total_i - match_i; ul_i_cnt = len(u_l_i); ub_i_cnt = len(u_b_i)
        st.markdown(f"""
        <div class="metric-row">
            <div class="metric-card"><div class="metric-value">{total_i}</div><div class="metric-label">Total Matched</div></div>
            <div class="metric-card green"><div class="metric-value">{match_i}</div><div class="metric-label">✅ Exact Match</div></div>
            <div class="metric-card red"><div class="metric-value">{mm_i}</div><div class="metric-label">❌ Mismatch</div></div>
            <div class="metric-card amber"><div class="metric-value">{ul_i_cnt}</div><div class="metric-label">Ledger Leftovers</div></div>
            <div class="metric-card blue"><div class="metric-value">{ub_i_cnt}</div><div class="metric-label">B2B Leftovers</div></div>
        </div>""", unsafe_allow_html=True)
        st.dataframe(style_status(m_i), use_container_width=True, hide_index=True)
        t1, t2 = st.tabs(["📋  Ledger Leftovers (Not in B2B)", "📋  B2B Leftovers (Not in Ledger)"])
        with t1:
            st.markdown(f"**Pending Ledger Leftovers:** `{len(u_l_i)} records`")
            show_cols = [c for c in u_l_i.columns if c not in ["VC Date", "VC No"]]
            st.dataframe(u_l_i[show_cols], use_container_width=True, hide_index=True)
        with t2:
            st.caption("These B2B invoices were not found in the company ledger.")
            b2b_cols = [
                "GSTIN of supplier", "Invoice number", "Trade/Legal name", "Invoice Date",
                "Invoice Value(₹)", "Integrated Tax(₹)", "Central Tax(₹)",
                "State/UT Tax(₹)", "GSTR-1/1A/IFF/GSTR-5 Filing Date"
            ]
            available = [c for c in b2b_cols if c in u_b_i.columns]
            st.dataframe(u_b_i[available], use_container_width=True, hide_index=True)
        st.markdown('<hr class="styled-divider">', unsafe_allow_html=True)

        # --- CGST ---
        st.markdown('<div class="tax-pill cgst">💜 CGST &nbsp;—&nbsp; Central Tax</div>', unsafe_allow_html=True)
        total_c = len(m_c); match_c = len(m_c[m_c['Status']=='✅ Match']) if not m_c.empty and 'Status' in m_c.columns else 0
        mm_c = total_c - match_c; ul_c_cnt = len(u_l_c); ub_c_cnt = len(u_b_c)
        st.markdown(f"""
        <div class="metric-row">
            <div class="metric-card"><div class="metric-value">{total_c}</div><div class="metric-label">Total Matched</div></div>
            <div class="metric-card green"><div class="metric-value">{match_c}</div><div class="metric-label">✅ Exact Match</div></div>
            <div class="metric-card red"><div class="metric-value">{mm_c}</div><div class="metric-label">❌ Mismatch</div></div>
            <div class="metric-card amber"><div class="metric-value">{ul_c_cnt}</div><div class="metric-label">Ledger Leftovers</div></div>
            <div class="metric-card blue"><div class="metric-value">{ub_c_cnt}</div><div class="metric-label">B2B Leftovers</div></div>
        </div>""", unsafe_allow_html=True)
        st.dataframe(style_status(m_c), use_container_width=True, hide_index=True)
        t1, t2 = st.tabs(["📋  Ledger Leftovers (Not in B2B)", "📋  B2B Leftovers (Not in Ledger)"])
        with t1:
            st.markdown(f"**Pending Ledger Leftovers:** `{len(u_l_c)} records`")
            show_cols = [c for c in u_l_c.columns if c not in ["VC Date", "VC No"]]
            st.dataframe(u_l_c[show_cols], use_container_width=True, hide_index=True)
        with t2:
            st.caption("These B2B invoices were not found in the company ledger.")
            b2b_cols = [
                "GSTIN of supplier", "Invoice number", "Trade/Legal name", "Invoice Date",
                "Invoice Value(₹)", "Integrated Tax(₹)", "Central Tax(₹)",
                "State/UT Tax(₹)", "GSTR-1/1A/IFF/GSTR-5 Filing Date"
            ]
            available = [c for c in b2b_cols if c in u_b_c.columns]
            st.dataframe(u_b_c[available], use_container_width=True, hide_index=True)
        st.markdown('<hr class="styled-divider">', unsafe_allow_html=True)

        # --- SGST ---
        st.markdown('<div class="tax-pill sgst">🌊 SGST &nbsp;—&nbsp; State/UT Tax</div>', unsafe_allow_html=True)
        total_s = len(m_s); match_s = len(m_s[m_s['Status']=='✅ Match']) if not m_s.empty and 'Status' in m_s.columns else 0
        mm_s = total_s - match_s; ul_s_cnt = len(u_l_s); ub_s_cnt = len(u_b_s)
        st.markdown(f"""
        <div class="metric-row">
            <div class="metric-card"><div class="metric-value">{total_s}</div><div class="metric-label">Total Matched</div></div>
            <div class="metric-card green"><div class="metric-value">{match_s}</div><div class="metric-label">✅ Exact Match</div></div>
            <div class="metric-card red"><div class="metric-value">{mm_s}</div><div class="metric-label">❌ Mismatch</div></div>
            <div class="metric-card amber"><div class="metric-value">{ul_s_cnt}</div><div class="metric-label">Ledger Leftovers</div></div>
            <div class="metric-card blue"><div class="metric-value">{ub_s_cnt}</div><div class="metric-label">B2B Leftovers</div></div>
        </div>""", unsafe_allow_html=True)
        st.dataframe(style_status(m_s), use_container_width=True, hide_index=True)
        t1, t2 = st.tabs(["📋  Ledger Leftovers (Not in B2B)", "📋  B2B Leftovers (Not in Ledger)"])
        with t1:
            st.markdown(f"**Pending Ledger Leftovers:** `{len(u_l_s)} records`")
            show_cols = [c for c in u_l_s.columns if c not in ["VC Date", "VC No"]]
            st.dataframe(u_l_s[show_cols], use_container_width=True, hide_index=True)
        with t2:
            st.caption("These B2B invoices were not found in the company ledger.")
            b2b_cols = [
                "GSTIN of supplier", "Invoice number", "Trade/Legal name", "Invoice Date",
                "Invoice Value(₹)", "Integrated Tax(₹)", "Central Tax(₹)",
                "State/UT Tax(₹)", "GSTR-1/1A/IFF/GSTR-5 Filing Date"
            ]
            available = [c for c in b2b_cols if c in u_b_s.columns]
            st.dataframe(u_b_s[available], use_container_width=True, hide_index=True)

        st.markdown('<hr class="styled-divider">', unsafe_allow_html=True)
        st.markdown("""
        <div class="section-header">
            <div class="section-icon green">📥</div>
            <div>
                <div class="section-title">Download Comprehensive Report</div>
                <div class="section-sub">Export all reconciliation results as a structured Excel workbook with 4 sheets</div>
            </div>
        </div>""", unsafe_allow_html=True)

        def generate_excel_report(m_i, m_c, m_s, u_l_i, u_l_c, u_l_s, u_b_i, u_b_c, u_b_s):
            output = io.BytesIO()
            matches_to_concat = [d for d in [m_i, m_c, m_s] if not d.empty]
            all_matches = pd.concat(matches_to_concat, ignore_index=True) if matches_to_concat else pd.DataFrame()

            if not all_matches.empty:
                pivot_df = all_matches.pivot_table(
                    index=['GSTIN', 'Bill/Inv No', 'Invoice Date', 'Filing Date'],
                    columns='Type',
                    values=['Ledger Amt', 'B2B Amt', 'Diff'],
                    aggfunc='sum'
                ).reset_index()
                pivot_df.columns = [f"{col[0]} {col[1]}".strip() if col[1] else col[0] for col in pivot_df.columns.values]
                pivot_df.fillna(0, inplace=True)

                diff_cols = [c for c in pivot_df.columns if 'Diff' in c]
                def get_status(row):
                    for c in diff_cols:
                        if pd.notna(row[c]) and abs(row[c]) >= 1:
                            return '❌ Mismatch'
                    return '✅ Match'
                pivot_df['Status'] = pivot_df.apply(get_status, axis=1)

                target_cols = [
                    'GSTIN', 'Bill/Inv No', 'Invoice Date', 'Filing Date',
                    'Ledger Amt IGST', 'B2B Amt IGST', 'Diff IGST',
                    'Ledger Amt CGST', 'B2B Amt CGST', 'Diff CGST',
                    'Ledger Amt SGST', 'B2B Amt SGST', 'Diff SGST',
                    'Status'
                ]
                for col in target_cols:
                    if col not in pivot_df.columns:
                        pivot_df[col] = 0.0 if col != 'Status' else ''
                pivot_df = pivot_df[target_cols]

                matched_sheet = pivot_df[pivot_df['Status'] == '✅ Match']
                mismatched_sheet = pivot_df[pivot_df['Status'] == '❌ Mismatch']
            else:
                matched_sheet = pd.DataFrame()
                mismatched_sheet = pd.DataFrame()

            u_l_all = []
            for df, t in [(u_l_i, 'IGST'), (u_l_c, 'CGST'), (u_l_s, 'SGST')]:
                if not df.empty:
                    df = df.copy()
                    df['Tax Type'] = t
                    u_l_all.append(df)
            ledger_only = pd.concat(u_l_all, ignore_index=True) if u_l_all else pd.DataFrame()

            u_b_to_concat = [d for d in [u_b_i, u_b_c, u_b_s] if not d.empty]
            u_b_all = pd.concat(u_b_to_concat, ignore_index=True) if u_b_to_concat else pd.DataFrame()
            if not u_b_all.empty:
                subset_cols = [c for c in ['GSTIN of supplier', 'Invoice number'] if c in u_b_all.columns]
                if subset_cols:
                    b2b_only = u_b_all.drop_duplicates(subset=subset_cols)
                else:
                    b2b_only = u_b_all
            else:
                b2b_only = pd.DataFrame()

            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                matched_sheet.to_excel(writer, sheet_name='Matched', index=False)
                mismatched_sheet.to_excel(writer, sheet_name='Mismatched', index=False)
                ledger_only.to_excel(writer, sheet_name='Ledger Only', index=False)
                b2b_only.to_excel(writer, sheet_name='B2B Only', index=False)

                # Auto-fit columns for all sheets
                for sheet_name in writer.sheets:
                    worksheet = writer.sheets[sheet_name]
                    for col in worksheet.columns:
                        max_length = 0
                        column = col[0].column_letter # Get the column name
                        for cell in col:
                            try:
                                if len(str(cell.value)) > max_length:
                                    max_length = len(str(cell.value))
                            except:
                                pass
                        adjusted_width = (max_length + 2)
                        worksheet.column_dimensions[column].width = adjusted_width

            return output.getvalue()

        excel_data = generate_excel_report(m_i, m_c, m_s, u_l_i, u_l_c, u_l_s, u_b_i, u_b_c, u_b_s)

        st.markdown("""
        <div class="download-banner">
            <div class="download-icon">📥</div>
            <div>
                <div class="download-text-title">Data Actions</div>
                <div class="download-text-sub">Export your data to Excel or sync all invoices centrally to the Supabase database.</div>
            </div>
        </div>""", unsafe_allow_html=True)

        st.download_button(
            label="📥  Download Excel Report",
            data=excel_data,
            file_name="GST_Reconciliation_Report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

        st.markdown('<hr class="styled-divider">', unsafe_allow_html=True)

        prev_col1, prev_col2 = st.columns([4, 1])
        with prev_col1:
            st.markdown("""
            <div class="section-header" style="margin-bottom: 0;">
                <div class="section-icon teal">💾</div>
                <div>
                    <div class="section-title">Database Sync Preview</div>
                    <div class="section-sub">Preview of the unified invoice records structured for Supabase storage</div>
                </div>
            </div>""", unsafe_allow_html=True)

        with prev_col2:
            st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)
            if st.button("☁️ Save to Database", use_container_width=True):
                save_all_to_supabase(m_i, m_c, m_s, u_l_i, u_l_c, u_l_s, u_b_i, u_b_c, u_b_s)
        db_preview = prepare_db_records(m_i, m_c, m_s, u_l_i, u_l_c, u_l_s, u_b_i, u_b_c, u_b_s)
        if not db_preview.empty:
            display_cols = [
                'GSTIN', 'Bill/Inv No', 'Invoice Date', 'Filing Date',
                'Ledger Amt IGST', 'B2B Amt IGST', 'Diff IGST',
                'Ledger Amt CGST', 'B2B Amt CGST', 'Diff CGST',
                'Ledger Amt SGST', 'B2B Amt SGST', 'Diff SGST',
                'Status'
            ]
            st.dataframe(style_status(db_preview[display_cols]), use_container_width=True, hide_index=True)
        else:
            st.info("No records to preview.")

    else:
        st.markdown("""
        <div style="
            background: #ffffff;
            border: 2px dashed #cbd5e1;
            border-radius: 16px;
            padding: 60px 40px;
            text-align: center;
            margin-top: 24px;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);
        ">
            <div style="font-size: 52px; margin-bottom: 16px;">📂</div>
            <div style="font-size: 22px; font-weight: 700; color: #0a2540; margin-bottom: 8px;">Waiting for Files</div>
            <div style="font-size: 15px; color: #475569;">Upload both the <strong style='color:#2563eb;'>Company Ledger</strong> and <strong style='color:#2563eb;'>GST Records</strong> files above to begin reconciliation.</div>
        </div>""", unsafe_allow_html=True)
with main_tab2:
    st.markdown('''
    <div class="section-header" style="margin-top: 10px;">
        <div class="section-icon teal">🗄️</div>
        <div>
            <div class="section-title">Central Database Dashboard</div>
            <div class="section-sub">View all synchronized records, matched invoices, and pending leftovers grouped by month</div>
        </div>
    </div>''', unsafe_allow_html=True)

    all_records = fetch_all_records_from_supabase()

    if all_records.empty:
        st.info("No records found in the central database.")
    else:
        # 1. Matched Records
        matched_df = all_records[all_records['status'] == '✅ Match'].copy()
        
        # 2. Pending Ledger
        pending_ledger_df = all_records[
            all_records['status'].isin(['❌ Missing (in B2B)', '❌ Mismatch (Amount)', '❌ Mismatch'])
        ].copy()
        
        # 3. Pending B2B
        pending_b2b_df = all_records[
            all_records['status'].isin(['❌ Missing (in Ledger)', '❌ Mismatch (Amount)', '❌ Mismatch'])
        ].copy()

        sub_tabs = st.tabs(["✅ Matched Records", "📋 Pending Ledger Data", "📋 Pending B2B Data"])
        
        # Helper for styling
        def highlight_status(df):
            if df.empty: return df
            if 'status' in df.columns:
                return df.style.map(
                    lambda v: 'color: #166534; font-weight: bold;' if '✅' in str(v) 
                    else ('color: #991b1b; font-weight: bold;' if '❌' in str(v) else ''),
                    subset=['status']
                )
            return df
        
        with sub_tabs[0]:
            st.subheader("Matched Invoices (by Reconciliation Month)")
            if 'reconciled_date' in matched_df.columns:
                matched_groups = group_by_month(matched_df, 'reconciled_date')
                if matched_groups:
                    sorted_months = sort_month_keys(matched_groups.keys())
                    selected_month = st.selectbox("Select Reconciliation Month", sorted_months, key="matched_month_select")
                    month_data = matched_groups[selected_month]
                    st.markdown(f"Showing **{len(month_data)}** matched invoices for **{selected_month}**.")
                    st.dataframe(highlight_status(month_data), use_container_width=True, hide_index=True, height=int(len(month_data) * 35.5) + 40)
                else:
                    st.info("No matched records found with valid dates.")
            else:
                st.info("No matched records found.")

        with sub_tabs[1]:
            st.subheader("Pending Ledger Invoices (by Bill Month)")
            if 'bill_date' in pending_ledger_df.columns:
                pending_l_groups = group_by_month(pending_ledger_df, 'bill_date')
                if pending_l_groups:
                    sorted_months = sort_month_keys(pending_l_groups.keys())
                    selected_month = st.selectbox("Select Ledger Bill Month", sorted_months, key="pending_l_month_select")
                    month_data = pending_l_groups[selected_month]
                    st.markdown(f"Showing **{len(month_data)}** pending ledger invoices for **{selected_month}**.")
                    st.dataframe(highlight_status(month_data), use_container_width=True, hide_index=True, height=int(len(month_data) * 35.5) + 40)
                else:
                    st.info("No pending ledger records found with valid dates.")
            else:
                st.info("No pending ledger records found.")

        with sub_tabs[2]:
            st.subheader("Pending B2B Invoices (by Invoice Month)")
            if 'invoice_date' in pending_b2b_df.columns:
                pending_b_groups = group_by_month(pending_b2b_df, 'invoice_date')
                if pending_b_groups:
                    sorted_months = sort_month_keys(pending_b_groups.keys())
                    selected_month = st.selectbox("Select B2B Invoice Month", sorted_months, key="pending_b_month_select")
                    month_data = pending_b_groups[selected_month]
                    st.markdown(f"Showing **{len(month_data)}** pending B2B invoices for **{selected_month}**.")
                    st.dataframe(highlight_status(month_data), use_container_width=True, hide_index=True, height=int(len(month_data) * 35.5) + 40)
                else:
                    st.info("No pending B2B records found with valid dates.")
            else:
                st.info("No pending B2B records found.")
